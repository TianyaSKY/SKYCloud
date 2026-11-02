"""文件分享:创建/取消分享链接,按 token 解析可下载文件(含过期校验)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.share import repository
from app.features.workspace.permissions import assert_member_locked
from app.infra.datetime_utils import to_utc_datetime, utc_now
from app.infra.storage import get_storage_client
from app.models.file import File
from app.models.share import Share, ShareDict

_DENIED = "Permission denied"


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateShareLinkParams:
    """创建分享链接所需的入参结构."""

    user_id: int
    file_id: int
    expires_at: datetime | None = None
    commit: bool = True
    workspace_id: int | None = None


def create_share_link(session: Session, params: CreateShareLinkParams) -> Share:
    """在调用方的事务内创建单条分享链接."""
    # REST 与 MCP 调用方均必须是文件所属工作区的当前有效成员。
    current_workspace_id = repository.file_workspace_id(session, params.file_id)
    if params.workspace_id is not None and current_workspace_id != params.workspace_id:
        raise PermissionDeniedError(_DENIED)
    if current_workspace_id is None:
        message = "File not found"
        raise ValueError(message)
    assert_member_locked(session, current_workspace_id, params.user_id)
    file = repository.lock_scoped_file(session, params.file_id, current_workspace_id)
    if file is None:
        message = "File not found"
        raise ValueError(message)

    share = Share(
        user_id=params.user_id,
        file_id=params.file_id,
        expires_at=to_utc_datetime(params.expires_at),
    )
    session.add(share)
    if params.commit:
        session.commit()
    else:
        session.flush()
    return share


def get_share_by_token(session: Session, token: str) -> Share | None:
    """按 token 取分享;过期视为无效返回 None."""
    share = session.query(Share).filter_by(token=token).first()
    if not share:
        return None

    expires_at = to_utc_datetime(share.expires_at)
    if expires_at and expires_at <= utc_now():
        return None

    return share


def get_my_shares(session: Session, user_id: int) -> list[ShareDict]:
    """用户分享列表,按创建时间倒序."""
    shares = (
        session.query(Share)
        .filter_by(user_id=user_id)
        .order_by(Share.created_at.desc())
        .all()
    )
    return [share.to_dict() for share in shares]


def cancel_share(session: Session, share_id: int, user_id: int) -> bool:
    """仅允许取消本人分享;不存在则返回 False."""
    share = session.query(Share).filter_by(id=share_id, user_id=user_id).first()
    if share:
        session.delete(share)
        session.commit()
        return True
    return False


def create_share(
    session: Session, user_id: int, file_id: int, expires_at_raw: str | None,
) -> Share:
    """解析 ISO 过期时间后创建分享;ValueError 转为业务异常."""
    expires_at = None
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError as exc:
            message = "Invalid date format"
            raise BusinessRuleError(message) from exc
    try:
        return create_share_link(
            session,
            CreateShareLinkParams(
                user_id=user_id, file_id=file_id, expires_at=expires_at,
            ),
        )
    except ValueError as exc:
        raise ResourceNotFoundError(str(exc)) from exc


def cancel_share_for_user(session: Session, share_id: int, user_id: int) -> None:
    """取消用户拥有的单条分享链接, 若不存在则报告缺失."""
    if not cancel_share(session, share_id, user_id):
        message = "Share not found or permission denied"
        raise ResourceNotFoundError(message)


def resolve_shared_file(session: Session, token: str) -> File:
    """公开下载入口:校验链接有效且对象存储中文件仍存在."""
    share = get_share_by_token(session, token)
    if not share:
        message = "Link invalid or expired"
        raise ResourceNotFoundError(message)
    file = share.file
    if not file:
        message = "File not found"
        raise ResourceNotFoundError(message)
    storage = get_storage_client()
    if not storage.file_exists(file.file_path):
        message = "File not found"
        raise ResourceNotFoundError(message)
    return file
