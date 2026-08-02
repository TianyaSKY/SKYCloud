"""Per-user, per-workspace OpenCode runtime records."""

from datetime import datetime
from typing import cast

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.infra.extensions import Base


class OpenCodeRuntime(Base):
    """A Dockerized OpenCode instance owned by one workspace member."""

    __tablename__ = "opencode_runtimes"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    container_id = Column(String(64), nullable=True)
    status = Column(String(20), nullable=False, default="stopped")
    error_message = Column(Text, nullable=True)
    config_version = Column(Integer, nullable=False, default=0)
    last_started_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=beijing_now)
    updated_at = Column(DateTime, default=beijing_now, onupdate=beijing_now)

    workspace = relationship("Workspace", back_populates="opencode_runtimes")
    user = relationship("User", backref="opencode_runtimes")
    tokens = relationship(
        "McpRuntimeToken",
        back_populates="runtime",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_opencode_runtime_workspace_user"),
        Index("idx_opencode_runtime_workspace", "workspace_id"),
        Index("idx_opencode_runtime_user", "user_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": cast(int | None, self.id),
            "workspace_id": cast(int | None, self.workspace_id),
            "user_id": cast(int | None, self.user_id),
            "container_id": self.container_id[:12] if self.container_id else None,
            "status": self.status,
            "error_message": self.error_message,
            "config_version": self.config_version,
            "last_started_at": local_isoformat(cast(datetime | None, self.last_started_at)),
            "last_used_at": local_isoformat(cast(datetime | None, self.last_used_at)),
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
            "updated_at": local_isoformat(cast(datetime | None, self.updated_at)),
        }
