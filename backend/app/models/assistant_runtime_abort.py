"""Durable, immutable remote-session cleanup after an assistant worker dies."""
import datetime

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime
from app.infra.datetime_utils import utc_now


class AssistantRuntimeAbort(Base):
    __tablename__ = 'assistant_runtime_aborts'
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_id: Mapped[int] = mapped_column(Integer, nullable=False)
    container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[str | None] = mapped_column(String(32))
    session_id: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(96))
    __table_args__ = (
        Index('ix_assistant_runtime_aborts_pending', 'workspace_id', 'user_id', postgresql_where=completed_at.is_(None)),
    )
