"""Per-user, per-workspace OpenCode runtime records."""
from app.infra.utc_datetime import UTCDateTime

from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import utc_now, local_isoformat
from app.infra.extensions import Base

if TYPE_CHECKING:
    from app.models.mcp_runtime_token import McpRuntimeToken
    from app.models.user import User
    from app.models.workspace import Workspace


class OpenCodeRuntime(Base):
    """A Dockerized OpenCode instance owned by one workspace member."""

    __tablename__ = "opencode_runtimes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="stopped")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="opencode_runtimes")
    user: Mapped["User"] = relationship("User", backref="opencode_runtimes")
    tokens: Mapped[list["McpRuntimeToken"]] = relationship(
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
