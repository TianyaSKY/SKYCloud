"""工作空间模型：协作空间与成员关系。

每个用户注册时自动创建一个私人工作空间，文件/文件夹归属于工作空间而非用户。
用户通过 workspace_members 表的成员关系访问工作空间内的资源。
"""
from app.infra.utc_datetime import UTCDateTime

from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import BigInteger, Integer, String, ForeignKey, UniqueConstraint, Index, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat

if TYPE_CHECKING:
    from app.models.opencode_runtime import OpenCodeRuntime
    from app.models.user import User


class Workspace(Base):
    """协作空间表：文件/文件夹的归属实体。

    每个协作空间可以按需关联一个 OpenCode Docker 容器。
    """

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    search_cache_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    description: Mapped[str | None] = mapped_column(String(512))
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)
    # OpenCode 容器生命周期。字段保持可空，兼容既有工作空间记录。
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="stopped")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 关系
    owner: Mapped["User"] = relationship("User", foreign_keys=[owner_id], backref="owned_workspaces")
    members: Mapped[list["WorkspaceMember"]] = relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan")
    opencode_runtimes: Mapped[list["OpenCodeRuntime"]] = relationship(
        "OpenCodeRuntime", back_populates="workspace", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Workspace {self.id} name={self.name} owner={self.owner_id}>"

    def to_dict(self):
        return {
            "id": cast(int | None, self.id),
            "name": cast(str, self.name),
            "description": cast(str | None, self.description),
            "owner_id": cast(int | None, self.owner_id),
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
            "updated_at": local_isoformat(cast(datetime | None, self.updated_at)),
            "container_id": self.container_id[:12] if self.container_id else None,
            "status": self.status,
            "error_message": self.error_message,
        }


class WorkspaceMember(Base):
    """工作空间成员表：用户与空间的多对多关系及角色。

    角色说明：
    - admin: 管理成员、删除空间、所有文件操作
    - editor: 上传/编辑/移动/删除文件
    - viewer: 只读（列表、下载、预览）
    """

    __tablename__ = "workspace_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")  # admin / editor / viewer
    invited_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    joined_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utc_now)

    # 关系
    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="members")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], backref="workspace_memberships")

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
        Index("idx_wm_user_id", "user_id"),
    )

    def __repr__(self):
        return f"<WorkspaceMember ws={self.workspace_id} user={self.user_id} role={self.role}>"

    def to_dict(self):
        return {
            "id": cast(int | None, self.id),
            "workspace_id": cast(int | None, self.workspace_id),
            "user_id": cast(int | None, self.user_id),
            "role": cast(str, self.role),
            "invited_by": cast(int | None, self.invited_by),
            "joined_at": local_isoformat(cast(datetime | None, self.joined_at)),
        }
