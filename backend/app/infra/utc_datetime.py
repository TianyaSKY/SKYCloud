"""UTC instants on PostgreSQL and SQLite, with explicit legacy input semantics."""
from datetime import timezone
from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

from app.infra.datetime_utils import to_utc_datetime


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def __init__(self, *, naive_is_utc=False):
        self.naive_is_utc = naive_is_utc
        super().__init__()

    def process_bind_param(self, value, dialect):
        value = to_utc_datetime(value, naive_is_utc=self.naive_is_utc)
        if value is not None and dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        # SQLite strips offsets; its persisted values are already UTC.
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
