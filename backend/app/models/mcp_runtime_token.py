"""Short-lived MCP tokens bound to one OpenCode runtime."""

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Index, Integer, String
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

# Tokens at or below this length are returned verbatim by the preview helper.
_TOKEN_PREVIEW_THRESHOLD = 16


class McpRuntimeTokenDict(TypedDict):
    """Public representation of a runtime-scoped MCP token row."""

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
    """Revocable token record for an OpenCode runtime."""

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
        """Return the SHA-256 digest of a token."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def preview_token(token: str) -> str:
        """Return a masked preview that keeps only the outer characters."""
        if len(token) <= _TOKEN_PREVIEW_THRESHOLD:
            return token
        return f"{token[:8]}...{token[-8:]}"

    @property
    def is_revoked(self) -> bool:
        """Return whether the token was explicitly revoked."""
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        """Return whether the token expiry instant has passed."""
        expires_at = to_utc_datetime(self.expires_at)
        return expires_at is not None and expires_at <= utc_now()

    @property
    def is_active(self) -> bool:
        """Return whether the token is neither revoked nor expired."""
        return not self.is_revoked and not self.is_expired

    def to_dict(self) -> McpRuntimeTokenDict:
        """Return the public runtime token representation."""
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
