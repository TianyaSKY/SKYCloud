from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import relationship, backref

from app.extensions import Base, _scoped_session


class Inbox(Base):
    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    type = Column(String(50), default="system")  # e.g., 'system', 'notification'
    created_at = Column(DateTime, default=datetime.utcnow)
    is_deleted = Column(Boolean, default=False)  # 是否删除占位

    # 添加 query 属性用于兼容 Flask-SQLAlchemy 风格的查询
    query = _scoped_session.query_property()

    # 关联用户
    user = relationship("User", backref=backref("inbox_messages", lazy="dynamic"))

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "content": self.content,
            "is_read": self.is_read,
            "type": self.type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
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
