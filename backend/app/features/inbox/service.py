"""站内信:创建、分页列表、已读与软删除."""

from typing import Required, TypedDict

from sqlalchemy.orm import Session

from app.exceptions import ResourceNotFoundError
from app.models.inbox import Inbox

_NOT_FOUND = "Inbox message not found"


class InboxCreateData(TypedDict, total=False):
    """创建收件箱站内通知时接受的字段定义."""

    user_id: Required[int | None]
    title: str | None
    content: Required[str]
    type: str


class InboxPage(TypedDict):
    """包含分页元数据的单页收件箱记录."""

    items: list[Inbox]
    total: int
    page: int
    per_page: int
    pages: int


def create_inbox_message(session: Session, data: InboxCreateData) -> Inbox:
    """创建并提交单条站内通知."""
    new_message = Inbox(
        user_id=data["user_id"],
        title=data.get("title"),
        content=data["content"],
        type=data.get("type", "system"),
    )
    session.add(new_message)
    session.commit()
    return new_message


def get_user_inbox(
    session: Session, user_id: int, page: int = 1, per_page: int = 20,
) -> InboxPage:
    """按用户分页拉取未删除消息."""
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


def get_inbox_message(session: Session, message_id: int) -> Inbox:
    """加载单条站内通知, 若不存在则抛出未找到异常."""
    message = session.get(Inbox, message_id)
    if not message:
        raise ResourceNotFoundError(_NOT_FOUND)
    return message


def mark_as_read(session: Session, message_id: int, user_id: int) -> Inbox:
    """将用户的单条消息标记为已读."""
    message = session.query(Inbox).filter_by(id=message_id, user_id=user_id).first()
    if not message:
        raise ResourceNotFoundError(_NOT_FOUND)
    message.is_read = True
    session.commit()
    return message


def delete_inbox_message(session: Session, message_id: int, user_id: int) -> None:
    """软删除用户的单条消息."""
    message = session.query(Inbox).filter_by(id=message_id, user_id=user_id).first()
    if not message:
        raise ResourceNotFoundError(_NOT_FOUND)
    message.is_deleted = True
    session.commit()


def mark_all_as_read(session: Session, user_id: int) -> None:
    """将当前用户的所有未读消息全部标记为已读."""
    session.query(Inbox).filter_by(user_id=user_id, is_read=False).update(
        {"is_read": True},
    )
    session.commit()
