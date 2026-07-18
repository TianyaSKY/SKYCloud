"""站内信：创建、分页列表、已读与软删除。"""

from app.exceptions import ResourceNotFoundError
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
    """按用户分页拉取未删除消息；返回结构兼容 Flask-SQLAlchemy Pagination。"""
    base_query = db.session.query(Inbox).filter_by(user_id=user_id, is_deleted=False).order_by(
        Inbox.created_at.desc()
    )

    total = base_query.count()
    offset = (page - 1) * per_page
    items = base_query.offset(offset).limit(per_page).all()

    # 兼容下游对 Pagination 字段的依赖（items/total/page/pages）
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page > 0 else 1,
    }


def get_inbox_message(id):
    message = db.session.get(Inbox, id)
    if not message:
        raise ResourceNotFoundError("Inbox message not found")
    return message


def mark_as_read(id, user_id):
    message = db.session.query(Inbox).filter_by(id=id, user_id=user_id).first()
    if not message:
        raise ResourceNotFoundError("Inbox message not found")
    message.is_read = True
    db.session.commit()
    return message


def delete_inbox_message(id, user_id):
    message = db.session.query(Inbox).filter_by(id=id, user_id=user_id).first()
    if not message:
        raise ResourceNotFoundError("Inbox message not found")
    message.is_deleted = True
    db.session.commit()


def mark_all_as_read(user_id):
    db.session.query(Inbox).filter_by(user_id=user_id, is_read=False).update({"is_read": True})
    db.session.commit()
