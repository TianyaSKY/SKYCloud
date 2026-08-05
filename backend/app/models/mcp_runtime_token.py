"""Short-lived MCP tokens bound to one OpenCode runtime."""

import hashlib
from datetime import datetime
from typing import cast

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from app.infra.datetime_utils import beijing_now, local_isoformat, to_beijing_naive
from app.infra.extensions import Base


class McpRuntimeToken(Base):
    """Revocable token record for an OpenCode runtime."""

    __tablename__ = "mcp_runtime_tokens"

    id = Column(Integer, primary_key=True)
    runtime_id = Column(
        Integer,
        ForeignKey("opencode_runtimes.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    jti = Column(String(64), nullable=False, unique=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    token_preview = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=beijing_now)
    expires_at = Column(DateTime, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    runtime = relationship("OpenCodeRuntime", back_populates="tokens")
    user = relationship("User", backref="mcp_runtime_tokens")

    __table_args__ = (
        Index("idx_mcp_runtime_tokens_runtime", "runtime_id"),
        Index("idx_mcp_runtime_tokens_user_workspace", "user_id", "workspace_id"),
    )

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def preview_token(token: str) -> str:
        if len(token) <= 16:
            return token
        return f"{token[:8]}...{token[-8:]}"

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        expires_at = to_beijing_naive(self.expires_at)
        return bool(expires_at and expires_at <= beijing_now())

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "runtime_id": self.runtime_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "jti": self.jti,
            "token_preview": self.token_preview,
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
            "expires_at": local_isoformat(cast(datetime | None, self.expires_at)),
            "last_used_at": local_isoformat(cast(datetime | None, self.last_used_at)),
            "revoked_at": local_isoformat(cast(datetime | None, self.revoked_at)),
            "is_revoked": self.is_revoked,
            "is_expired": self.is_expired,
        }
