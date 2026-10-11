"""Object cleanup intents committed atomically with file metadata deletion."""
import datetime

from sqlalchemy import Index, Integer, Text, String
from sqlalchemy.orm import Mapped, mapped_column
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime
from app.infra.datetime_utils import utc_now


class FileObjectDeletion(Base):
    __tablename__ = "file_object_deletions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(96))
    __table_args__ = (Index("ix_file_object_deletions_pending", "id",
                           postgresql_where=completed_at.is_(None)),)
