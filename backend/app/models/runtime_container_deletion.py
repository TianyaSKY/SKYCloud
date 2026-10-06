"""Container cleanup intents survive deletion of their account/workspace."""
from sqlalchemy import Column, Index, Integer, String
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime
from app.infra.datetime_utils import utc_now


class RuntimeContainerDeletion(Base):
    __tablename__ = 'runtime_container_deletions'
    id = Column(Integer, primary_key=True)
    container_id = Column(String(64), nullable=False)
    created_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    completed_at = Column(UTCDateTime())
    attempts = Column(Integer, nullable=False, default=0)
    last_error_type = Column(String(96))
    __table_args__ = (Index('ix_runtime_container_deletions_pending', 'id', postgresql_where=completed_at.is_(None)),)
