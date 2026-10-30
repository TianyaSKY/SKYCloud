"""Public inbox response models."""

from pydantic import BaseModel


class InboxMessage(BaseModel):
    """One inbox notification row."""

    id: int
    user_id: int
    title: str | None
    content: str
    is_read: bool
    type: str | None
    created_at: str | None


class InboxPage(BaseModel):
    """One page of inbox notifications."""

    items: list[InboxMessage]
    total: int
    pages: int
    current_page: int


class InboxAcknowledgment(BaseModel):
    """Acknowledgment returned after marking messages read."""

    message: str
