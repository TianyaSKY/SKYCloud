"""用于文件整理扫描和审计的文件与文件夹变更日志模型."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class FileChangeEventDict(TypedDict):
    """文件变更事件记录的公开表示模型."""

    id: int
    workspace_id: int
    entity_type: str
    entity_id: int
    action: str
    old_parent_id: int | None
    new_parent_id: int | None
    old_name: str | None
    new_name: str | None
    payload: str | None
    created_at: str | None


class FileChangeEvent(Base):
    """用于增量文件整理扫描和审计的变更日志条目."""

    __tablename__ = "file_change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    old_parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )

    workspace: Mapped["Workspace"] = relationship(
        "Workspace",
        backref="file_change_events",
    )

    __table_args__ = (
        Index("idx_file_change_events_ws_created", "workspace_id", "created_at"),
        Index("idx_file_change_events_ws_id", "workspace_id", "id"),
    )

    def to_dict(self) -> FileChangeEventDict:
        """返回变更事件的公开字典表示."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "action": self.action,
            "old_parent_id": self.old_parent_id,
            "new_parent_id": self.new_parent_id,
            "old_name": self.old_name,
            "new_name": self.new_name,
            "payload": self.payload,
            "created_at": local_isoformat(self.created_at),
        }
