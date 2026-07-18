import hashlib
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.infra.datetime_utils import beijing_now, local_isoformat, to_beijing_naive
from app.extensions import Base


class McpToken(Base):
    """每用户有且仅有一条有效 MCP Token（revoked_at IS NULL）。

    token_value 保存完整 JWT，供：
    - 前端随时复制
    - 工作区自动注入 opencode MCP 配置
    token_hash 仍用于鉴权查找。
    """

    __tablename__ = "mcp_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False, default="MCP Token")
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    token_preview = Column(String(32), nullable=False)
    # 完整 JWT，服务端复用；历史行可能为 NULL，需 refresh 补齐
    token_value = Column(Text, nullable=True)
    created_at = Column(DateTime, default=beijing_now)
    expires_at = Column(DateTime, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User", backref="mcp_tokens")

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

    def to_dict(self, include_token: bool = False):
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
        if include_token and self.token_value:
            data["mcp_token"] = self.token_value
        return data
