"""用户账户模型: 登录身份、角色权限与累计 Token 消耗统计."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import BigInteger, Enum, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from app.infra.datetime_utils import (
    local_isoformat,
    parse_legacy_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.api.stream_credentials import StreamCredential


class UserDict(TypedDict):
    """用户账户记录的公开表示模型."""

    id: int
    username: str
    role: str | None
    avatar: str | None
    created_at: str | None
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    last_active_at: str | None


class User(Base):
    """用户账户模型: 包含登录标识、角色及 Token 使用总量."""

    __tablename__ = "users"

    # 声明请求局部的流式凭据字段, 但不进行数据库映射。
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(1024), nullable=False)
    auth_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    role: Mapped[str | None] = mapped_column(
        Enum(
            "admin",
            "common",
            name="user_roles",
            native_enum=False,
            length=10,
        ),
        default="common",
        server_default="common",
    )
    avatar: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )

    # ---- Token 使用量统计总计, 由 LLM 调用详情汇总写入 ----
    total_prompt_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    total_completion_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    total_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    last_active_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=None,
    )

    # 用于流式响应持续鉴权的请求局部凭据。由 HTTP
    # 凭据依赖项注入, 绝不持久化或序列化。
    http_stream_credential: "StreamCredential | None" = None

    def __repr__(self) -> str:
        """返回该用户的调试打印字符串."""
        return f"<User {self.username}>"

    def set_password(self, password: str) -> None:
        """为给定的明文密码计算并存储安全密码哈希."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """校验输入的密码明文是否与存储的哈希匹配."""
        return check_password_hash(self.password_hash, password)

    def to_dict(self) -> UserDict:
        """返回用户的公开字典表示."""
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
    def from_cache(cls, d: UserDict) -> "User":
        """根据缓存的档案字段构建游离态用户记录."""
        return cls(
            id=d.get("id"),
            username=d.get("username"),
            role=d.get("role"),
            avatar=d.get("avatar"),
            created_at=parse_legacy_datetime(d.get("created_at")),
            total_prompt_tokens=d.get("total_prompt_tokens", 0),
            total_completion_tokens=d.get("total_completion_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
            last_active_at=parse_legacy_datetime(d.get("last_active_at")),
        )
