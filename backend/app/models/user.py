from app.infra.utc_datetime import UTCDateTime

import datetime

from sqlalchemy import Integer, BigInteger, String, Enum
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import generate_password_hash, check_password_hash

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat, parse_legacy_datetime


class User(Base):
    """用户账户表：登录身份、角色与 Token 用量累计。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(1024), nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    role: Mapped[str | None] = mapped_column(Enum("admin", "common", name="user_roles", native_enum=False, length=10), default="common")
    avatar: Mapped[str | None] = mapped_column(String(255), default=None)  # 头像 URL，可为空
    created_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(), default=utc_now)

    # ---- Token 用量累计（由 LLM 调用明细汇总写入）----
    total_prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, default=0)
    total_completion_tokens: Mapped[int | None] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, default=0)
    last_active_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(), default=None)

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
        created_at = parse_legacy_datetime(created_at_str)
        last_active_str = d.get("last_active_at")
        last_active_at = parse_legacy_datetime(last_active_str)
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
