from fastapi import HTTPException

from app.extensions import db
from app.models.inbox import Inbox


def create_inbox_message(data):
    new_message = Inbox(
        user_id=data.get("user_id"),
        title=data.get("title"),
        content=data.get("content"),
        type=data.get("type", "system"),
    )
    db.session.add(new_message)
    db.session.commit()
    return new_message


def get_user_inbox(user_id, page=1, per_page=20):
    """获取用户收件箱消息，手动分页"""
    base_query = Inbox.query.filter_by(user_id=user_id, is_deleted=False).order_by(
        Inbox.created_at.desc()
    )

    total = base_query.count()
    offset = (page - 1) * per_page
    items = base_query.offset(offset).limit(per_page).all()

    # 返回类似 Flask-SQLAlchemy Pagination 的结构
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page > 0 else 1,
    }


def get_inbox_message(id):
    message = Inbox.query.get(id)
    if not message:
        raise HTTPException(status_code=404, detail="Inbox message not found")
    return message


def mark_as_read(id, user_id):
    message = Inbox.query.filter_by(id=id, user_id=user_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Inbox message not found")
    message.is_read = True
    db.session.commit()
    return message


def delete_inbox_message(id, user_id):
    message = Inbox.query.filter_by(id=id, user_id=user_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Inbox message not found")
    message.is_deleted = True
    db.session.commit()


def mark_all_as_read(user_id):
    Inbox.query.filter_by(user_id=user_id, is_read=False).update({"is_read": True})
    db.session.commit()
