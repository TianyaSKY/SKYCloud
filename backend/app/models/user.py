from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, Enum
from sqlalchemy.orm import relationship
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import Base, _scoped_session


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(1024), nullable=False)
    role = Column(Enum("admin", "common", name="user_roles"), default="common")
    avatar = Column(String(255), default=None)  # 头像 URL，默认为空
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # 添加 query 属性用于兼容 Flask-SQLAlchemy 风格的查询
    query = _scoped_session.query_property()

    def __repr__(self):
        return f"<User {self.username}>"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "avatar": self.avatar,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_cache(cls, d: dict) -> "User":
        created_at_str = d.get("created_at")
        created_at = datetime.fromisoformat(created_at_str) if created_at_str else None
        return cls(
            id=d.get("id"),
            username=d.get("username"),
            role=d.get("role"),
            avatar=d.get("avatar"),
            created_at=created_at,
        )
