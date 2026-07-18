"""文件分享：创建/取消分享链接，按 token 解析可下载文件（含过期校验）。"""

import os
from datetime import datetime

from app.exceptions import BusinessRuleError, ResourceNotFoundError
from app.extensions import db
from app.infra.datetime_utils import beijing_now, to_beijing_naive
from app.models.file import File
from app.models.share import Share


def create_share_link(user_id, file_id, expires_at=None):
    # 当前简化：仅校验文件存在，未强制归属（调用方应先做权限）
    file = db.session.get(File, file_id)
    if not file:
        raise ValueError("File not found")

    share = Share(
        user_id=user_id,
        file_id=file_id,
        expires_at=expires_at
    )
    db.session.add(share)
    db.session.commit()
    return share


def get_share_by_token(token):
    """按 token 取分享；过期视为无效返回 None。"""
    share = db.session.query(Share).filter_by(token=token).first()
    if not share:
        return None

    expires_at = to_beijing_naive(share.expires_at)
    if expires_at and expires_at < beijing_now():
        return None

    return share


def get_my_shares(user_id):
    """用户分享列表，按创建时间倒序。"""
    shares = db.session.query(Share).filter_by(user_id=user_id).order_by(Share.created_at.desc()).all()
    return [share.to_dict() for share in shares]


def cancel_share(share_id, user_id):
    """仅允许取消本人分享；不存在则返回 False。"""
    share = db.session.query(Share).filter_by(id=share_id, user_id=user_id).first()
    if share:
        db.session.delete(share)
        db.session.commit()
        return True
    return False


def create_share(user_id: int, file_id: int, expires_at_raw: str | None) -> Share:
    """解析 ISO 过期时间后创建分享；ValueError 转为业务异常。"""
    expires_at = None
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError as exc:
            raise BusinessRuleError("Invalid date format") from exc
    try:
        return create_share_link(user_id, file_id, expires_at)
    except ValueError as exc:
        raise ResourceNotFoundError(str(exc)) from exc


def cancel_share_for_user(share_id: int, user_id: int) -> None:
    if not cancel_share(share_id, user_id):
        raise ResourceNotFoundError("Share not found or permission denied")


def resolve_shared_file(token: str) -> File:
    """公开下载入口：校验链接有效且磁盘文件仍存在。"""
    share = get_share_by_token(token)
    if not share:
        raise ResourceNotFoundError("Link invalid or expired")
    file = share.file
    if not file or not os.path.exists(file.get_abs_path()):
        raise ResourceNotFoundError("File not found")
    return file
