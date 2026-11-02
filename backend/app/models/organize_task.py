"""持久化的文件整理任务准入、投递与执行标识模型."""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class OrganizeTask(Base):
    """包含投递与执行状态标识的持久化文件整理任务记录."""

    __tablename__ = "organize_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="queued",
    )
    execution_token: Mapped[str | None] = mapped_column(String(64))
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
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(96))
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        Index(
            "uq_organize_active_workspace",
            "workspace_id",
            unique=True,
            postgresql_where=state.in_(["queued", "running"]),
            sqlite_where=state.in_(["queued", "running"]),
        ),
    )
