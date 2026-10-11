"""File indexing deliveries committed alongside file metadata."""
from app.infra.utc_datetime import UTCDateTime
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.extensions import Base


def utc_now():
    return datetime.now(timezone.utc)


class FileTaskOutbox(Base):
    __tablename__ = "file_task_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(96))

    __table_args__ = (
        Index("ix_file_task_outbox_pending", "id", postgresql_where=delivered_at.is_(None)),
    )
