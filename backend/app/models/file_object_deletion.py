"""Object cleanup intents committed atomically with file metadata deletion."""
from sqlalchemy import Column, Index, Integer, Text, String
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime
from app.infra.datetime_utils import utc_now


class FileObjectDeletion(Base):
    __tablename__ = "file_object_deletions"
    id = Column(Integer, primary_key=True)
    object_name = Column(Text, nullable=False)
    created_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    completed_at = Column(UTCDateTime())
    attempts = Column(Integer, nullable=False, default=0)
    last_error_type = Column(String(96))
    __table_args__ = (Index("ix_file_object_deletions_pending", "id",
                           postgresql_where=completed_at.is_(None)),)
