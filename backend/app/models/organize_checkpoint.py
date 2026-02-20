from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlalchemy.orm import relationship

from app.extensions import Base, _scoped_session


class OrganizeCheckpoint(Base):
    __tablename__ = "organize_checkpoints"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    last_event_id = Column(Integer, nullable=False, default=0)
    last_full_scan_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    query = _scoped_session.query_property()
    user = relationship("User", backref="organize_checkpoint")

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "last_event_id": self.last_event_id,
            "last_full_scan_at": self.last_full_scan_at.isoformat()
            if self.last_full_scan_at
            else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
