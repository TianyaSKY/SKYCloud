"""与文件元数据一同提交的文件索引任务发件箱模型."""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class FileTaskOutbox(Base):
    """用于单个文件索引投递的事务型发件箱记录."""

    __tablename__ = "file_task_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_error_type: Mapped[str | None] = mapped_column(String(96))

    __table_args__ = (
        Index(
            "ix_file_task_outbox_pending",
            "id",
            postgresql_where=delivered_at.is_(None),
        ),
    )
