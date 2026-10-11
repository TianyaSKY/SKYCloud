"""Durable upload capacity reservations and UTC daily admission usage."""
import datetime
from typing import Any

from app.infra.utc_datetime import UTCDateTime
from sqlalchemy import BigInteger, Date, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.infra.extensions import Base


class UploadReservation(Base):
    __tablename__ = 'upload_reservations'
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='active')
    created_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime(naive_is_utc=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime(naive_is_utc=True), nullable=False)
    cleaned_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(naive_is_utc=True), nullable=True)
    details: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    __table_args__ = (
        Index('idx_upload_user_active', 'user_id', 'status', 'expires_at'),
        Index('idx_upload_workspace_active', 'workspace_id', 'status', 'expires_at'),
    )


class UploadDailyUsage(Base):
    __tablename__ = 'upload_daily_usage'
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    day: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
