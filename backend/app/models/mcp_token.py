"""绑定至特定用户的长期 MCP 个人访问令牌模型."""

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import (
    local_isoformat,
    to_utc_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.user import User


class McpTokenDict(TypedDict):
    """长期 MCP 令牌记录的公开表示模型."""

    id: int
    user_id: int
    name: str
    token_preview: str
    created_at: str | None
    expires_at: str | None
    last_used_at: str | None
    revoked_at: str | None
    is_revoked: bool
    is_expired: bool


class McpToken(Base):
    """每个用户至多拥有一个有效记录的长期 MCP 令牌模型.

    仅持久化存储 SHA-256 哈希值与脱敏指纹。原始明文凭证仅在显式签发操作时返回一次。
    """

    __tablename__ = "mcp_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_mcp_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="MCP Token",
        server_default="MCP Token",
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
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

    user: Mapped["User"] = relationship("User", backref="mcp_tokens")

    __table_args__ = (
        Index("idx_mcp_tokens_token_hash", "token_hash"),
        Index("idx_mcp_tokens_user_id", "user_id"),
        Index(
            "uq_mcp_tokens_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    @staticmethod
    def hash_token(token: str) -> str:
        """返回令牌明文的 SHA-256 摘要值."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def preview_token(token: str) -> str:
        """返回根据令牌哈希派生的指纹脱敏预览."""
        return f"sha256:{McpToken.hash_token(token)[:12]}"

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

    def to_dict(self) -> McpTokenDict:
        """返回不包含原始明文凭证的公开令牌字典表示."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "token_preview": self.token_preview,
            "created_at": local_isoformat(self.created_at),
            "expires_at": local_isoformat(self.expires_at),
            "last_used_at": local_isoformat(self.last_used_at),
            "revoked_at": local_isoformat(self.revoked_at),
            "is_revoked": self.is_revoked,
            "is_expired": self.is_expired,
        }
