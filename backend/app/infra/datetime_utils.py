from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """Return current Beijing time as a naive datetime for existing DB columns."""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def to_beijing_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def local_isoformat(value: datetime | None) -> str | None:
    value = to_beijing_naive(value)
    return value.isoformat() if value else None
