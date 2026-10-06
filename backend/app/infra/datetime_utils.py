"""UTC instants with explicit conversion for legacy Beijing inputs and presentation."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_datetime(value: datetime | None, *, naive_is_utc=False) -> datetime | None:
    """Legacy naive inputs mean Beijing time; UTC-naive reservation inputs are explicit."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc if naive_is_utc else BEIJING_TZ)
    return value.astimezone(timezone.utc)


def beijing_now() -> datetime:
    """当前北京时间（去掉 tzinfo），兼容现有 DB 列。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def to_beijing_naive(value: datetime | None) -> datetime | None:
    """将 aware datetime 转为北京 naive；已是 naive 则原样返回。"""
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
