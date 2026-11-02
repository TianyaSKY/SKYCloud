"""公开的收件箱响应数据模型."""

from pydantic import BaseModel


class InboxMessage(BaseModel):
    """单条收件箱站内通知记录."""

    id: int
    user_id: int
    title: str | None
    content: str
    is_read: bool
    type: str | None
    created_at: str | None


class InboxPage(BaseModel):
    """单页收件箱站内通知列表."""

    items: list[InboxMessage]
    total: int
    pages: int
    current_page: int


class InboxAcknowledgment(BaseModel):
    """标记消息已读后返回的确认响应模型."""

    message: str
