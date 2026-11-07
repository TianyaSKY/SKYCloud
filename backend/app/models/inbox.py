"""支持按用户已读状态与软删除机制的收件箱站内通知模型."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, backref, mapped_column, relationship

from app.infra.datetime_utils import (
    local_isoformat,
    parse_legacy_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.user import User


class InboxDict(TypedDict):
    """站内通知记录的公开表示模型."""

    id: int
    user_id: int
    title: str | None
    content: str
    is_read: bool | None
    type: str | None
    created_at: str | None


class Inbox(Base):
    """带有已读标记与软删除特性的站内通知模型."""

    __tablename__ = "inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool | None] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    type: Mapped[str | None] = mapped_column(
        String(50),
        default="system",
        server_default="system",
    )
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )
    is_deleted: Mapped[bool | None] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )

    user: Mapped["User"] = relationship(
        "User",
        backref=backref("inbox_messages", lazy="dynamic"),
    )

    def to_dict(self) -> InboxDict:
        """返回站内通知的公开字典表示."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "content": self.content,
            "is_read": self.is_read,
            "type": self.type,
            "created_at": local_isoformat(self.created_at),
        }

    @classmethod
    def from_cache(cls, d: InboxDict) -> "Inbox":
        """根据缓存字段构建游离态通知记录."""
        return cls(
            id=d.get("id"),
            user_id=d.get("user_id"),
            title=d.get("title"),
            content=d.get("content"),
            is_read=d.get("is_read"),
            type=d.get("type"),
            created_at=parse_legacy_datetime(d.get("created_at")),
        )
