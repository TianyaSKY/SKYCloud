from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.extensions import Base


class FileChangeEvent(Base):
    __tablename__ = "file_change_events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    entity_type = Column(String(20), nullable=False)  # file / folder
    entity_id = Column(Integer, nullable=False)
    action = Column(String(32), nullable=False)  # create / move / rename / delete / update_meta
    old_parent_id = Column(Integer, nullable=True)
    new_parent_id = Column(Integer, nullable=True)
    old_name = Column(String(255), nullable=True)
    new_name = Column(String(255), nullable=True)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime, default=beijing_now, nullable=False, index=True)


    user = relationship("User", backref="file_change_events")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
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
