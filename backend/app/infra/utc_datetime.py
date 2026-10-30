"""UTC instants on PostgreSQL and SQLite, with explicit legacy input semantics."""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.infra.datetime_utils import to_utc_datetime


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC instants while preserving the configured legacy naive input policy."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def __init__(self, *, naive_is_utc: bool = False) -> None:
        """Configure whether naive input represents UTC rather than Beijing time."""
        self.naive_is_utc = naive_is_utc
        super().__init__()

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """Convert inputs to UTC and strip offsets only for SQLite storage."""
        value = to_utc_datetime(value, naive_is_utc=self.naive_is_utc)
        if value is not None and dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """Restore persisted instants as aware UTC datetimes on every dialect."""
        del dialect
        if value is None:
            return None
        # SQLite strips offsets; its persisted values are already UTC.
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
