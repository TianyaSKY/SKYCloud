"""按用户、按工作区隔离的 OpenCode 运行时实例记录模型."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.mcp_runtime_token import McpRuntimeToken
    from app.models.user import User
    from app.models.workspace import Workspace


class OpenCodeRuntimeDict(TypedDict):
    """OpenCode 运行时记录的公开表示模型."""

    id: int
    workspace_id: int
    user_id: int
    container_id: str | None
    status: str
    error_message: str | None
    config_version: int
    last_started_at: str | None
    last_used_at: str | None
    created_at: str | None
    updated_at: str | None


class OpenCodeRuntime(Base):
    """由单个工作区成员拥有的容器化 OpenCode 实例记录."""

    __tablename__ = "opencode_runtimes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    container_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="stopped",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    last_started_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
    )

    workspace: Mapped["Workspace"] = relationship(
        "Workspace",
        back_populates="opencode_runtimes",
    )
    user: Mapped["User"] = relationship("User", backref="opencode_runtimes")
    tokens: Mapped[list["McpRuntimeToken"]] = relationship(
        "McpRuntimeToken",
        back_populates="runtime",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "user_id",
            name="uq_opencode_runtime_workspace_user",
        ),
        Index("idx_opencode_runtime_workspace", "workspace_id"),
        Index("idx_opencode_runtime_user", "user_id"),
    )

    def to_dict(self) -> OpenCodeRuntimeDict:
        """返回运行时实例的公开字典表示."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "container_id": self.container_id[:12] if self.container_id else None,
            "status": self.status,
            "error_message": self.error_message,
            "config_version": self.config_version,
            "last_started_at": local_isoformat(self.last_started_at),
            "last_used_at": local_isoformat(self.last_used_at),
            "created_at": local_isoformat(self.created_at),
            "updated_at": local_isoformat(self.updated_at),
        }
