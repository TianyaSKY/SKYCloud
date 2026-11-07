"""工作区拥有的文件夹目录树节点模型."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict, cast

from sqlalchemy import ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.exceptions import BusinessRuleError, PermissionDeniedError
from app.infra.datetime_utils import (
    local_isoformat,
    parse_legacy_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.file import File


class FolderDict(TypedDict):
    """文件夹记录的公开表示模型."""

    id: int
    name: str
    workspace_id: int
    parent_id: int | None
    path: str
    created_at: str | None


class Folder(Base):
    """文件夹树节点; 删除节点将级联删除其所有子树.

    文件夹归属于工作区而非具体用户.
    """

    __tablename__ = "folder"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("folder.id", ondelete="CASCADE"),
    )
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )

    parent: Mapped["Folder | None"] = relationship(
        "Folder",
        remote_side=[id],
        back_populates="sub_folder",
    )
    sub_folder: Mapped[list["Folder"]] = relationship(
        "Folder",
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    # 数据库级联删除文件元数据; 物理存储清理在服务层执行。
    files: Mapped[list["File"]] = relationship(
        "File",
        backref="folder",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_folder_workspace_id", "workspace_id"),
        Index("idx_folder_ws_parent", "workspace_id", "parent_id"),
    )

    def to_dict(self) -> FolderDict:
        """返回包含完整路径的公开文件夹字典表示."""
        path_parts: list[str] = []
        current: Folder | None = self
        seen: set[tuple[str, int]] = set()
        while current is not None:
            # 瞬态记录尚未分配主键 ID; 回退至使用对象内存标识。
            row_id = cast("int | None", current.id)
            identity: tuple[str, int] = (
                ("row", row_id) if row_id is not None else ("object", id(current))
            )
            if identity in seen:
                message = "Invalid folder hierarchy"
                raise BusinessRuleError(message)
            seen.add(identity)
            if current.workspace_id != self.workspace_id:
                message = "Parent folder belongs to another workspace"
                raise PermissionDeniedError(message)
            if current.parent_id is None:
                break
            path_parts.append(current.name)
            current = current.parent

        path = "/" + "/".join(reversed(path_parts))
        return {
            "id": self.id,
            "name": "/" if self.parent_id is None else self.name,
            "workspace_id": self.workspace_id,
            "parent_id": self.parent_id,
            "path": path,
            "created_at": local_isoformat(self.created_at),
        }

    @classmethod
    def from_cache(cls, d: FolderDict) -> "Folder":
        """根据缓存的元数据字段构建游离态文件夹记录."""
        return cls(
            id=d.get("id"),
            name=d.get("name"),
            workspace_id=d.get("workspace_id"),
            parent_id=d.get("parent_id"),
            created_at=parse_legacy_datetime(d.get("created_at")),
        )
