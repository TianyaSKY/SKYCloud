"""Durable upload capacity reservations and UTC daily admission usage."""
from app.infra.utc_datetime import UTCDateTime
from sqlalchemy import BigInteger, Column, Date, ForeignKey, Index, Integer, JSON, String
from app.infra.extensions import Base


class UploadReservation(Base):
    __tablename__ = 'upload_reservations'
    id = Column(String(128), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    workspace_id = Column(Integer, ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False)
    size = Column(BigInteger, nullable=False)
    kind = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default='active')
    created_at = Column(UTCDateTime(naive_is_utc=True), nullable=False)
    expires_at = Column(UTCDateTime(naive_is_utc=True), nullable=False)
    cleaned_at = Column(UTCDateTime(naive_is_utc=True), nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    __table_args__ = (
        Index('idx_upload_user_active', 'user_id', 'status', 'expires_at'),
        Index('idx_upload_workspace_active', 'workspace_id', 'status', 'expires_at'),
    )


class UploadDailyUsage(Base):
    __tablename__ = 'upload_daily_usage'
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    day = Column(Date, primary_key=True)
    attempts = Column(Integer, nullable=False, default=0)
    bytes = Column(BigInteger, nullable=False, default=0)
