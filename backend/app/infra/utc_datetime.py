"""PostgreSQL 与 SQLite 上的 UTC 时间戳, 具备显式遗留输入语义."""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.infra.datetime_utils import to_utc_datetime


class UTCDateTime(TypeDecorator[datetime]):
    """存储 UTC 时间戳, 同时保留配置的遗留 naive 输入策略."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def __init__(self, *, naive_is_utc: bool = False) -> None:
        """配置 naive 输入是否代表 UTC 时间而非北京时间."""
        self.naive_is_utc = naive_is_utc
        super().__init__()

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """将输入转换为 UTC, 且仅在 SQLite 存储时剥离偏移量."""
        value = to_utc_datetime(value, naive_is_utc=self.naive_is_utc)
        if value is not None and dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """在所有方言上将持久化的时间点恢复为有时区的 UTC datetime."""
        del dialect
        if value is None:
            return None
        # SQLite 会剥离偏移量; 其持久化的值已是 UTC 时间。
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
