"""文件分享：创建/取消分享链接，按 token 解析可下载文件（含过期校验）。"""

import os
from datetime import datetime

from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError, ResourceNotFoundError
from app.infra.datetime_utils import beijing_now, to_beijing_naive
from app.models.file import File
from app.models.share import Share


def create_share_link(session: Session, user_id, file_id, expires_at=None):
    # 当前简化：仅校验文件存在，未强制归属（调用方应先做权限）
    file = session.get(File, file_id)
    if not file:
        raise ValueError("File not found")

    share = Share(
        user_id=user_id,
        file_id=file_id,
        expires_at=expires_at
    )
    session.add(share)
    session.commit()
    return share


def get_share_by_token(session: Session, token):
    """按 token 取分享；过期视为无效返回 None。"""
    share = session.query(Share).filter_by(token=token).first()
    if not share:
        return None

    expires_at = to_beijing_naive(share.expires_at)
    if expires_at and expires_at < beijing_now():
        return None

    return share


def get_my_shares(session: Session, user_id):
    """用户分享列表，按创建时间倒序。"""
    shares = (
        session.query(Share)
        .filter_by(user_id=user_id)
        .order_by(Share.created_at.desc())
        .all()
    )
    return [share.to_dict() for share in shares]


def cancel_share(session: Session, share_id, user_id):
    """仅允许取消本人分享；不存在则返回 False。"""
    share = session.query(Share).filter_by(id=share_id, user_id=user_id).first()
    if share:
        session.delete(share)
        session.commit()
        return True
    return False


def create_share(session: Session, user_id: int, file_id: int, expires_at_raw: str | None) -> Share:
    """解析 ISO 过期时间后创建分享；ValueError 转为业务异常。"""
    expires_at = None
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError as exc:
            raise BusinessRuleError("Invalid date format") from exc
    try:
        return create_share_link(session, user_id, file_id, expires_at)
    except ValueError as exc:
        raise ResourceNotFoundError(str(exc)) from exc


def cancel_share_for_user(session: Session, share_id: int, user_id: int) -> None:
    if not cancel_share(session, share_id, user_id):
        raise ResourceNotFoundError("Share not found or permission denied")


def resolve_shared_file(session: Session, token: str) -> File:
    """公开下载入口：校验链接有效且磁盘文件仍存在。"""
    share = get_share_by_token(session, token)
    if not share:
        raise ResourceNotFoundError("Link invalid or expired")
    file = share.file
    if not file or not os.path.exists(file.get_abs_path()):
        raise ResourceNotFoundError("File not found")
    return file
