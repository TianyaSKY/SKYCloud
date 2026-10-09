"""Durable, immutable remote-session cleanup after an assistant worker dies."""
from sqlalchemy import Column, Index, Integer, String
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime
from app.infra.datetime_utils import utc_now


class AssistantRuntimeAbort(Base):
    __tablename__ = 'assistant_runtime_aborts'
    run_id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    runtime_id = Column(Integer, nullable=False)
    container_id = Column(String(64), nullable=False)
    generation = Column(String(32))
    session_id = Column(String(128))
    lease_token = Column(String(32))
    created_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    completed_at = Column(UTCDateTime())
    attempts = Column(Integer, nullable=False, default=0)
    last_error_type = Column(String(96))
    __table_args__ = (
        Index('ix_assistant_runtime_aborts_pending', 'workspace_id', 'user_id', postgresql_where=completed_at.is_(None)),
    )
