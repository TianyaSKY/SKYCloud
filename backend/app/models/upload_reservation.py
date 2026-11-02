"""持久化上传容量预留与基于 UTC 天维度的准入限额模型."""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class UploadReservation(Base):
    """单次暂存分片上传或普通上传的预留容量记录."""

    __tablename__ = "upload_reservations"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(naive_is_utc=True),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(naive_is_utc=True),
        nullable=False,
    )
    cleaned_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(naive_is_utc=True),
        nullable=True,
    )
    details: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    __table_args__ = (
        Index("idx_upload_user_active", "user_id", "status", "expires_at"),
        Index(
            "idx_upload_workspace_active",
            "workspace_id",
            "status",
            "expires_at",
        ),
    )


class UploadDailyUsage(Base):
    """单个用户在特定 UTC 日期内的每日上传准入计数器."""

    __tablename__ = "upload_daily_usage"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
