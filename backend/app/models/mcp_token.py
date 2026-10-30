"""Long-lived MCP tokens bound to one user."""

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Integer, String
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
    """Public representation of a long-lived MCP token row."""

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
    """Long-lived MCP token row with at most one active record per user.

    Only the SHA-256 hash and fingerprint are persisted. Raw credentials are
    returned once by the explicit issuance operation.
    """

    __tablename__ = "mcp_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="MCP Token",
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
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

    user: Mapped["User"] = relationship("User", backref="mcp_tokens")

    @staticmethod
    def hash_token(token: str) -> str:
        """Return the SHA-256 digest of a token."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def preview_token(token: str) -> str:
        """Return a fingerprint preview derived from the token hash."""
        return f"sha256:{McpToken.hash_token(token)[:12]}"

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

    def to_dict(self) -> McpTokenDict:
        """Return the public token representation without the raw credential."""
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
