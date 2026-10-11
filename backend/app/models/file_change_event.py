import datetime

from app.infra.utc_datetime import UTCDateTime
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import TYPE_CHECKING

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class FileChangeEvent(Base):
    """文件/目录变更事件日志：供整理任务增量扫描与审计。"""

    __tablename__ = "file_change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)  # file / folder
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # create / move / rename / delete / update_meta
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    old_parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # 可选扩展 JSON/文本
    created_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False, index=True)

    workspace: Mapped["Workspace"] = relationship("Workspace", backref="file_change_events")

    def to_dict(self) -> dict:
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
