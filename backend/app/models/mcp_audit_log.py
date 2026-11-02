"""MCP 工具调用的审计日志记录模型."""

from datetime import datetime
from typing import TypedDict

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class McpAuditLogDict(TypedDict):
    """MCP 审计日志记录的公开表示模型."""

    id: int
    request_id: str
    runtime_id: int | None
    workspace_id: int | None
    user_id: int | None
    role: str | None
    actor_type: str
    tool_name: str
    arguments_summary: str | None
    entity_id: int | None
    success: bool
    error_type: str | None
    duration_ms: int | None
    created_at: str | None


class McpAuditLog(Base):
    """单个 MCP 工具调用的审计记录."""

    __tablename__ = "mcp_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("opencode_runtimes.id", ondelete="SET NULL"),
        nullable=True,
    )
    workspace_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="user",
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )

    __table_args__ = (
        Index("idx_mcp_audit_workspace_created", "workspace_id", "created_at"),
        Index("idx_mcp_audit_runtime_created", "runtime_id", "created_at"),
        Index("idx_mcp_audit_user_created", "user_id", "created_at"),
    )

    def to_dict(self) -> McpAuditLogDict:
        """返回审计记录的公开字典表示."""
        return {
            "id": self.id,
            "request_id": self.request_id,
            "runtime_id": self.runtime_id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "role": self.role,
            "actor_type": self.actor_type,
            "tool_name": self.tool_name,
            "arguments_summary": self.arguments_summary,
            "entity_id": self.entity_id,
            "success": self.success,
            "error_type": self.error_type,
            "duration_ms": self.duration_ms,
            "created_at": local_isoformat(self.created_at),
        }
