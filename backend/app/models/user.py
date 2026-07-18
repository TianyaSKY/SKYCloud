from datetime import datetime

from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Enum
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import Base
from app.infra.datetime_utils import beijing_now, local_isoformat


class User(Base):
    """用户账户表：登录身份、角色与 Token 用量累计。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(1024), nullable=False)
    role = Column(Enum("admin", "common", name="user_roles"), default="common")
    avatar = Column(String(255), default=None)  # 头像 URL，可为空
    created_at = Column(DateTime, default=beijing_now)

    # ---- Token 用量累计（由 LLM 调用明细汇总写入）----
    total_prompt_tokens = Column(BigInteger, default=0)
    total_completion_tokens = Column(BigInteger, default=0)
    total_tokens = Column(BigInteger, default=0)
    last_active_at = Column(DateTime, default=None)

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
            "created_at": local_isoformat(self.created_at),
            "total_prompt_tokens": self.total_prompt_tokens or 0,
            "total_completion_tokens": self.total_completion_tokens or 0,
            "total_tokens": self.total_tokens or 0,
            "last_active_at": local_isoformat(self.last_active_at),
        }

    @classmethod
    def from_cache(cls, d: dict) -> "User":
        created_at_str = d.get("created_at")
        created_at = datetime.fromisoformat(created_at_str) if created_at_str else None
        last_active_str = d.get("last_active_at")
        last_active_at = datetime.fromisoformat(last_active_str) if last_active_str else None
        return cls(
            id=d.get("id"),
            username=d.get("username"),
            role=d.get("role"),
            avatar=d.get("avatar"),
            created_at=created_at,
            total_prompt_tokens=d.get("total_prompt_tokens", 0),
            total_completion_tokens=d.get("total_completion_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
            last_active_at=last_active_at,
        )
