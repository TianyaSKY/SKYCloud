"""绑定至单个 OpenCode 运行时实例的短期 MCP 令牌模型."""

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import (
    local_isoformat,
    to_utc_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.opencode_runtime import OpenCodeRuntime
    from app.models.user import User

# 小于或等于该长度的令牌在脱敏预览时按原样返回。
_TOKEN_PREVIEW_THRESHOLD = 16


class McpRuntimeTokenDict(TypedDict):
    """限定于运行时作用域的 MCP 令牌公开表示模型."""

    id: int
    runtime_id: int
    user_id: int
    workspace_id: int
    jti: str
    token_preview: str
    created_at: str | None
    expires_at: str | None
    last_used_at: str | None
    revoked_at: str | None
    is_revoked: bool
    is_expired: bool


class McpRuntimeToken(Base):
    """用于 OpenCode 运行时的可撤销令牌记录模型."""

    __tablename__ = "mcp_runtime_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    runtime_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("opencode_runtimes.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    token_preview: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    runtime: Mapped["OpenCodeRuntime"] = relationship(
        "OpenCodeRuntime",
        back_populates="tokens",
    )
    user: Mapped["User"] = relationship("User", backref="mcp_runtime_tokens")

    __table_args__ = (
        Index("idx_mcp_runtime_tokens_runtime", "runtime_id"),
        Index("idx_mcp_runtime_tokens_user_workspace", "user_id", "workspace_id"),
    )

    @staticmethod
    def hash_token(token: str) -> str:
        """返回令牌明文的 SHA-256 摘要值."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def preview_token(token: str) -> str:
        """返回仅保留首尾字符的脱敏预览字符串."""
        if len(token) <= _TOKEN_PREVIEW_THRESHOLD:
            return token
        return f"{token[:8]}...{token[-8:]}"

    @property
    def is_revoked(self) -> bool:
        """返回该令牌是否已被显式撤销."""
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        """返回令牌是否已过有效期截止时间点."""
        expires_at = to_utc_datetime(self.expires_at)
        return expires_at is not None and expires_at <= utc_now()

    @property
    def is_active(self) -> bool:
        """返回该令牌是否未撤销且未过期 (有效状态)."""
        return not self.is_revoked and not self.is_expired

    def to_dict(self) -> McpRuntimeTokenDict:
        """返回运行时令牌的公开字典表示."""
        return {
            "id": self.id,
            "runtime_id": self.runtime_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "jti": self.jti,
            "token_preview": self.token_preview,
            "created_at": local_isoformat(self.created_at),
            "expires_at": local_isoformat(self.expires_at),
            "last_used_at": local_isoformat(self.last_used_at),
            "revoked_at": local_isoformat(self.revoked_at),
            "is_revoked": self.is_revoked,
            "is_expired": self.is_expired,
        }
