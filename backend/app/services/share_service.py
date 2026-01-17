from datetime import datetime

from app.extensions import db
from app.models.file import File
from app.models.share import Share


def create_share_link(user_id, file_id, expires_at=None):
    # 检查文件是否存在且属于该用户（或管理员逻辑，此处简化）
    file = File.query.get(file_id)
    if not file:
        raise ValueError("File not found")

    # 创建分享记录
    share = Share(
        user_id=user_id,
        file_id=file_id,
        expires_at=expires_at
    )
    db.session.add(share)
    db.session.commit()
    return share


def get_share_by_token(token):
    share = Share.query.filter_by(token=token).first()
    if not share:
        return None

    # 检查是否过期
    if share.expires_at and share.expires_at < datetime.utcnow():
        return None

    return share


def get_my_shares(user_id):
    """
    获取用户的所有分享记录
    """
    shares = Share.query.filter_by(user_id=user_id).order_by(Share.created_at.desc()).all()
    return [share.to_dict() for share in shares]


def cancel_share(share_id, user_id):
    """
    取消分享
    """
    share = Share.query.filter_by(id=share_id, user_id=user_id).first()
    if share:
        db.session.delete(share)
        db.session.commit()
        return True
    return False
