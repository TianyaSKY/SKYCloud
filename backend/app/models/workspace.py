"""工作区模型: 团队协作空间与成员关系定义.

每个用户注册时均分配一个私有工作区。文件和文件夹
归属于工作区而非具体用户; 用户通过 workspace_members
成员关系表访问对应的工作区资源。
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.opencode_runtime import OpenCodeRuntime
    from app.models.user import User


class WorkspaceDict(TypedDict):
    """工作区记录的公开表示模型."""

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
    """工作区成员关联记录的公开表示模型."""

    id: int
    workspace_id: int
    user_id: int
    role: str
    invited_by: int | None
    joined_at: str | None


class Workspace(Base):
    """拥有文件、文件夹及运行时实例的协作空间模型."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    search_cache_revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    description: Mapped[str | None] = mapped_column(String(512))
    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_workspaces_owner_id_users",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    # OpenCode 容器生命周期标识; 兼容存量历史工作区可为空。
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="stopped",
        server_default="stopped",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_workspaces_owner_id", "owner_id"),)

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
        """返回该工作区的调试打印字符串."""
        return f"<Workspace {self.id} name={self.name} owner={self.owner_id}>"

    def to_dict(self) -> WorkspaceDict:
        """返回工作区的公开字典表示."""
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
    """将用户与工作区按特定角色关联的成员关系模型.

    角色说明: admin 管理成员及所有文件操作, editor 可上传/编辑/移动/删除文件,
    viewer 为只读权限。
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
        server_default="viewer",
    )
    invited_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_workspace_members_invited_by_users",
            ondelete="SET NULL",
        ),
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
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
        Index("idx_wm_workspace_id", "workspace_id"),
    )

    def __repr__(self) -> str:
        """返回该成员关联记录的调试打印字符串."""
        return (
            f"<WorkspaceMember ws={self.workspace_id} "
            f"user={self.user_id} role={self.role}>"
        )

    def to_dict(self) -> WorkspaceMemberDict:
        """返回成员关联记录的公开字典表示."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "role": self.role,
            "invited_by": self.invited_by,
            "joined_at": local_isoformat(self.joined_at),
        }
