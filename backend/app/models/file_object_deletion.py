"""与文件元数据删除原子性提交的对象存储物理清理意图模型."""

from datetime import datetime

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class FileObjectDeletion(Base):
    """针对已删除文件的待处理对象存储清理记录."""

    __tablename__ = "file_object_deletions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_name: Mapped[str] = mapped_column(Text, nullable=False)
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
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(96))

    __table_args__ = (
        Index(
            "ix_file_object_deletions_pending",
            "id",
            postgresql_where=completed_at.is_(None),
        ),
    )
