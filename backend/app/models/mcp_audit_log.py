"""Audit records for MCP tool calls."""

from datetime import datetime
from typing import cast

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.infra.extensions import Base


class McpAuditLog(Base):
    __tablename__ = "mcp_audit_logs"

    id = Column(Integer, primary_key=True)
    request_id = Column(String(64), nullable=False)
    runtime_id = Column(
        Integer,
        ForeignKey("opencode_runtimes.id", ondelete="SET NULL"),
        nullable=True,
    )
    workspace_id = Column(
        Integer,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    role = Column(String(20), nullable=True)
    actor_type = Column(String(20), nullable=False, default="user")
    tool_name = Column(String(100), nullable=False)
    arguments_summary = Column(Text, nullable=True)
    entity_id = Column(Integer, nullable=True)
    success = Column(Boolean, nullable=False, default=False)
    error_type = Column(String(100), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=beijing_now)

    __table_args__ = (
        Index("idx_mcp_audit_workspace_created", "workspace_id", "created_at"),
        Index("idx_mcp_audit_runtime_created", "runtime_id", "created_at"),
        Index("idx_mcp_audit_user_created", "user_id", "created_at"),
    )

    def to_dict(self) -> dict:
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
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
        }
