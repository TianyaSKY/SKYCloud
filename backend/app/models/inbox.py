from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import relationship, backref

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.extensions import Base


class Inbox(Base):
    """站内信/通知表：系统消息与用户可读状态（支持软删除）。"""

    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    type = Column(String(50), default="system")  # 如 system、notification
    created_at = Column(DateTime, default=beijing_now)
    is_deleted = Column(Boolean, default=False)  # 软删除标记

    user = relationship("User", backref=backref("inbox_messages", lazy="dynamic"))

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
            created_at=datetime.fromisoformat(d.get("created_at"))
            if d.get("created_at")
            else None,
        )
