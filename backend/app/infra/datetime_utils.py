"""UTC instants with explicit conversion for legacy Beijing inputs and presentation."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


def to_utc_datetime(
    value: datetime | None,
    *,
    naive_is_utc: bool = False,
) -> datetime | None:
    """Treat legacy naive inputs as Beijing time unless UTC is explicit."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC if naive_is_utc else BEIJING_TZ)
    return value.astimezone(UTC)


def beijing_now() -> datetime:
    """当前北京时间(去掉 tzinfo), 兼容现有 DB 列."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def to_beijing_naive(value: datetime | None) -> datetime | None:
    """将 aware datetime 转为北京 naive; 已是 naive 则原样返回."""
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def local_isoformat(value: datetime | None) -> str | None:
    """Serialize an instant as explicit UTC; legacy naive DB values are Beijing time."""
    value = to_utc_datetime(value)
    return value.isoformat() if value is not None else None


def parse_legacy_datetime(value: str | None) -> datetime | None:
    """Read old naive or new offset-bearing cache dates into UTC instants."""
    return to_utc_datetime(datetime.fromisoformat(value)) if value else None
