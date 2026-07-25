"""工作空间模型：协作空间与成员关系。

每个用户注册时自动创建一个私人工作空间，文件/文件夹归属于工作空间而非用户。
用户通过 workspace_members 表的成员关系访问工作空间内的资源。
"""

from datetime import datetime
from typing import cast

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import relationship

from app.infra.extensions import Base
from app.infra.datetime_utils import beijing_now, local_isoformat


class Workspace(Base):
    """协作空间表：文件/文件夹的归属实体。

    预留字段说明：未来合并 Docker 工作区时可增加 container_id, status, access_url 等。
    """

    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    description = Column(String(512))
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=beijing_now)
    updated_at = Column(DateTime, default=beijing_now, onupdate=beijing_now)

    # 关系
    owner = relationship("User", foreign_keys=[owner_id], backref="owned_workspaces")
    members = relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan")

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
        }


class WorkspaceMember(Base):
    """工作空间成员表：用户与空间的多对多关系及角色。

    角色说明：
    - admin: 管理成员、删除空间、所有文件操作
    - editor: 上传/编辑/移动/删除文件
    - viewer: 只读（列表、下载、预览）
    """

    __tablename__ = "workspace_members"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False, default="viewer")  # admin / editor / viewer
    invited_by = Column(Integer, ForeignKey("users.id"))
    joined_at = Column(DateTime, default=beijing_now)

    # 关系
    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User", foreign_keys=[user_id], backref="workspace_memberships")

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
