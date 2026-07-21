"""站内信：创建、分页列表、已读与软删除。"""

from sqlalchemy.orm import Session

from app.exceptions import ResourceNotFoundError
from app.models.inbox import Inbox


def create_inbox_message(session: Session, data):
    new_message = Inbox(
        user_id=data.get("user_id"),
        title=data.get("title"),
        content=data.get("content"),
        type=data.get("type", "system"),
    )
    session.add(new_message)
    session.commit()
    return new_message


def get_user_inbox(session: Session, user_id, page=1, per_page=20):
    """按用户分页拉取未删除消息。"""
    base_query = (
        session.query(Inbox)
        .filter_by(user_id=user_id, is_deleted=False)
        .order_by(Inbox.created_at.desc())
    )

    total = base_query.count()
    offset = (page - 1) * per_page
    items = base_query.offset(offset).limit(per_page).all()

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page > 0 else 1,
    }


def get_inbox_message(session: Session, id):
    message = session.get(Inbox, id)
    if not message:
        raise ResourceNotFoundError("Inbox message not found")
    return message


def mark_as_read(session: Session, id, user_id):
    message = session.query(Inbox).filter_by(id=id, user_id=user_id).first()
    if not message:
        raise ResourceNotFoundError("Inbox message not found")
    message.is_read = True
    session.commit()
    return message


def delete_inbox_message(session: Session, id, user_id):
    message = session.query(Inbox).filter_by(id=id, user_id=user_id).first()
    if not message:
        raise ResourceNotFoundError("Inbox message not found")
    message.is_deleted = True
    session.commit()


def mark_all_as_read(session: Session, user_id):
    session.query(Inbox).filter_by(user_id=user_id, is_read=False).update({"is_read": True})
    session.commit()
