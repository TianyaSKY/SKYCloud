"""Runtime replacement identities survive deletion of their owning rows."""
from sqlalchemy import Column, Index, Integer, String, CheckConstraint

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now
from app.infra.utc_datetime import UTCDateTime


class RuntimeOperation(Base):
    __tablename__ = 'runtime_operations'

    generation = Column(String(32), primary_key=True)
    workspace_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    runtime_id = Column(Integer, nullable=False)
    previous_container_id = Column(String(64))
    candidate_container_id = Column(String(64))
    state = Column(String(20), nullable=False, default='preparing')
    created_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    next_attempt_at = Column(UTCDateTime(), nullable=False, default=utc_now)
    finished_at = Column(UTCDateTime())
    attempts = Column(Integer, nullable=False, default=0)
    last_error_type = Column(String(96))

    __table_args__ = (
        Index('ix_runtime_operations_candidate', 'candidate_container_id'),
        CheckConstraint("state IN ('preparing','switched','complete','cancelled')", name='ck_runtime_operation_state'),
        Index('uq_runtime_operation_active', 'runtime_id', unique=True,
              postgresql_where=state.in_(['preparing', 'switched']),
              sqlite_where=state.in_(['preparing', 'switched'])),
        Index('ix_runtime_operations_pending', 'next_attempt_at', 'generation',
              postgresql_where=finished_at.is_(None), sqlite_where=finished_at.is_(None)),
    )
