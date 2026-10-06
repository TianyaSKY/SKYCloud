from app.infra.utc_datetime import UTCDateTime
import hashlib

from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat, to_utc_datetime


class McpToken(Base):
    """MCP 长效 Token 表：每用户至多一条有效记录（revoked_at IS NULL）。

    Only the SHA-256 hash and fingerprint are persisted. Raw credentials are
    returned once by the explicit issuance operation.
    """

    __tablename__ = "mcp_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False, default="MCP Token")
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    token_preview = Column(String(32), nullable=False)
    created_at = Column(UTCDateTime(), default=utc_now)
    expires_at = Column(UTCDateTime(), nullable=False)
    last_used_at = Column(UTCDateTime(), nullable=True)
    revoked_at = Column(UTCDateTime(), nullable=True)

    user = relationship("User", backref="mcp_tokens")

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def preview_token(token: str) -> str:
        return f"sha256:{McpToken.hash_token(token)[:12]}"

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        expires_at = to_utc_datetime(self.expires_at)
        return bool(expires_at and expires_at <= utc_now())

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired

    def to_dict(self):
        data = {
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
        return data
