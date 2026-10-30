"""Workspace models: collaboration spaces and member relationships.

Every user receives a private workspace at registration. Files and folders
belong to a workspace rather than to a user; users reach workspace resources
through the workspace_members rows.
"""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.opencode_runtime import OpenCodeRuntime
    from app.models.user import User


class WorkspaceDict(TypedDict):
    """Public representation of a workspace row."""

    id: int
    name: str
    description: str | None
    owner_id: int
    created_at: str | None
    updated_at: str | None
    container_id: str | None
    status: str
    error_message: str | None


class WorkspaceMemberDict(TypedDict):
    """Public representation of a workspace membership row."""

    id: int
    workspace_id: int
    user_id: int
    role: str
    invited_by: int | None
    joined_at: str | None


class Workspace(Base):
    """Collaboration space owning files, folders, and runtimes."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    search_cache_revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    description: Mapped[str | None] = mapped_column(String(512))
    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
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
    # OpenCode container lifecycle; nullable for pre-existing workspaces.
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="stopped",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner: Mapped["User"] = relationship(
        "User",
        foreign_keys=[owner_id],
        backref="owned_workspaces",
    )
    members: Mapped[list["WorkspaceMember"]] = relationship(
        "WorkspaceMember",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    opencode_runtimes: Mapped[list["OpenCodeRuntime"]] = relationship(
        "OpenCodeRuntime",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        """Return a debug representation of this workspace."""
        return f"<Workspace {self.id} name={self.name} owner={self.owner_id}>"

    def to_dict(self) -> WorkspaceDict:
        """Return the public workspace representation."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "owner_id": self.owner_id,
            "created_at": local_isoformat(self.created_at),
            "updated_at": local_isoformat(self.updated_at),
            "container_id": self.container_id[:12] if self.container_id else None,
            "status": self.status,
            "error_message": self.error_message,
        }


class WorkspaceMember(Base):
    """Membership row linking a user to a workspace with a role.

    Roles: admin manages members and every file operation, editor may
    upload/edit/move/delete files, and viewer is read-only.
    """

    __tablename__ = "workspace_members"

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
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="viewer",
    )
    invited_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )

    workspace: Mapped["Workspace"] = relationship(
        "Workspace",
        back_populates="members",
    )
    user: Mapped["User"] = relationship(
        "User",
        foreign_keys=[user_id],
        backref="workspace_memberships",
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "user_id",
            name="uq_workspace_member",
        ),
        Index("idx_wm_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        """Return a debug representation of this membership."""
        return (
            f"<WorkspaceMember ws={self.workspace_id} "
            f"user={self.user_id} role={self.role}>"
        )

    def to_dict(self) -> WorkspaceMemberDict:
        """Return the public membership representation."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "role": self.role,
            "invited_by": self.invited_by,
            "joined_at": local_isoformat(self.joined_at),
        }
