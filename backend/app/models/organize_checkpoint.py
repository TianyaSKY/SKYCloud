"""每个工作区的文件自动整理进度检查点模型."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Integer, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class OrganizeCheckpointDict(TypedDict):
    """整理检查点记录的公开表示模型."""

    workspace_id: int
    last_event_id: int
    last_full_scan_at: str | None
    updated_at: str | None


class OrganizeCheckpoint(Base):
    """每个工作区已消费的变更事件位点与上次全量扫描记录."""

    __tablename__ = "organize_checkpoints"

    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_event_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_full_scan_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    workspace: Mapped["Workspace"] = relationship(
        "Workspace",
        backref="organize_checkpoint",
    )

    def to_dict(self) -> OrganizeCheckpointDict:
        """返回整理检查点的公开字典表示."""
        return {
            "workspace_id": self.workspace_id,
            "last_event_id": self.last_event_id,
            "last_full_scan_at": local_isoformat(self.last_full_scan_at),
            "updated_at": local_isoformat(self.updated_at),
        }
