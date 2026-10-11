"""Durable organize admission, delivery and execution identity."""
from app.infra.utc_datetime import UTCDateTime
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.extensions import Base


def utc_now():
    return datetime.now(timezone.utc)


class OrganizeTask(Base):
    __tablename__ = "organize_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    execution_token: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(96))
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        Index("uq_organize_active_workspace", "workspace_id", unique=True,
              postgresql_where=state.in_(["queued", "running"]),
              sqlite_where=state.in_(["queued", "running"])),
    )
