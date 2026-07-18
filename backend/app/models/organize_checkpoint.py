from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlalchemy.orm import relationship

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.extensions import Base


class OrganizeCheckpoint(Base):
    """目录整理进度检查点：记录用户已消费的变更事件位点与全量扫描时间。"""

    __tablename__ = "organize_checkpoints"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    last_event_id = Column(Integer, nullable=False, default=0)  # 已处理的最大 event id
    last_full_scan_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=beijing_now, onupdate=beijing_now)

    user = relationship("User", backref="organize_checkpoint")

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "last_event_id": self.last_event_id,
            "last_full_scan_at": local_isoformat(self.last_full_scan_at),
            "updated_at": local_isoformat(self.updated_at),
        }
