import datetime
from typing import TYPE_CHECKING

from app.infra.utc_datetime import UTCDateTime
from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class OrganizeCheckpoint(Base):
    """目录整理进度检查点：记录工作空间已消费的变更事件位点与全量扫描时间。"""

    __tablename__ = "organize_checkpoints"

    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), primary_key=True)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 已处理的最大 event id
    last_full_scan_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

    workspace: Mapped["Workspace"] = relationship("Workspace", backref="organize_checkpoint")

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "last_event_id": self.last_event_id,
            "last_full_scan_at": local_isoformat(self.last_full_scan_at),
            "updated_at": local_isoformat(self.updated_at),
        }
