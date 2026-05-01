from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlalchemy.orm import relationship

from app.datetime_utils import beijing_now, local_isoformat
from app.extensions import Base


class OrganizeCheckpoint(Base):
    __tablename__ = "organize_checkpoints"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    last_event_id = Column(Integer, nullable=False, default=0)
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
