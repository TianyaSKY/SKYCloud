"""Durable organize admission, delivery and execution identity."""
from app.infra.utc_datetime import UTCDateTime
from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Index, Integer, String

from app.infra.extensions import Base


def utc_now():
    return datetime.now(timezone.utc)


class OrganizeTask(Base):
    __tablename__ = "organize_tasks"

    id = Column(String(64), primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    state = Column(String(20), nullable=False, default="queued")
    execution_token = Column(String(64))
    created_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    delivered_at = Column(UTCDateTime())
    attempts = Column(Integer, nullable=False, default=0)
    last_error_type = Column(String(96))
    finished_at = Column(UTCDateTime())

    __table_args__ = (
        Index("uq_organize_active_workspace", "workspace_id", unique=True,
              postgresql_where=state.in_(["queued", "running"]),
              sqlite_where=state.in_(["queued", "running"])),
    )
