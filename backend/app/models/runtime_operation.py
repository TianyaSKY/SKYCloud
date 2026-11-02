"""在所属主体记录删除后依然保留的运行时重建标识模型."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class RuntimeOperation(Base):
    """受世代令牌栅栏保护的运行时重建操作标识."""

    __tablename__ = "runtime_operations"

    generation: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_id: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_container_id: Mapped[str | None] = mapped_column(String(64))
    candidate_container_id: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="preparing",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(96))

    __table_args__ = (
        Index("ix_runtime_operations_candidate", "candidate_container_id"),
        CheckConstraint(
            "state IN ('preparing','switched','complete','cancelled')",
            name="ck_runtime_operation_state",
        ),
        Index(
            "uq_runtime_operation_active",
            "runtime_id",
            unique=True,
            postgresql_where=state.in_(["preparing", "switched"]),
            sqlite_where=state.in_(["preparing", "switched"]),
        ),
        Index(
            "ix_runtime_operations_pending",
            "next_attempt_at",
            "generation",
            postgresql_where=finished_at.is_(None),
            sqlite_where=finished_at.is_(None),
        ),
    )
