from app.infra.utc_datetime import UTCDateTime

import datetime

from sqlalchemy import Integer, String, ForeignKey, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref
from typing import TYPE_CHECKING

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat, parse_legacy_datetime

if TYPE_CHECKING:
    from app.models.user import User


class Inbox(Base):
    """站内信/通知表：系统消息与用户可读状态（支持软删除）。"""

    __tablename__ = "inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool | None] = mapped_column(Boolean, default=False)
    type: Mapped[str | None] = mapped_column(String(50), default="system")  # 如 system、notification
    created_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(), default=utc_now)
    is_deleted: Mapped[bool | None] = mapped_column(Boolean, default=False)  # 软删除标记

    user: Mapped["User"] = relationship("User", backref=backref("inbox_messages", lazy="dynamic"))

    def to_dict(self):
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
    def from_cache(cls, d: dict) -> "Inbox":
        return cls(
            id=d.get("id"),
            user_id=d.get("user_id"),
            title=d.get("title"),
            content=d.get("content"),
            is_read=d.get("is_read"),
            type=d.get("type"),
            created_at=parse_legacy_datetime(d.get("created_at")),
        )
