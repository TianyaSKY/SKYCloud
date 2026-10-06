"""File indexing deliveries committed alongside file metadata."""
from app.infra.utc_datetime import UTCDateTime
from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Index, Integer, String

from app.infra.extensions import Base


def utc_now():
    return datetime.now(timezone.utc)


class FileTaskOutbox(Base):
    __tablename__ = "file_task_outbox"

    id = Column(Integer, primary_key=True)
    file_id = Column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    delivered_at = Column(UTCDateTime())
    attempts = Column(Integer, nullable=False, default=0)
    last_error_type = Column(String(96))

    __table_args__ = (
        Index("ix_file_task_outbox_pending", "id", postgresql_where=delivered_at.is_(None)),
    )
