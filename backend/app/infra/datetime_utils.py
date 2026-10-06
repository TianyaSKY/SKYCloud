"""北京时区时间工具：DB 列多为无时区 naive datetime，统一按 Asia/Shanghai 处理。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


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
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(timezone.utc).isoformat()


def parse_legacy_datetime(value: str | None) -> datetime | None:
    """Read old naive or new offset-bearing cache dates into legacy SQL wall time."""
    return to_beijing_naive(datetime.fromisoformat(value)) if value else None
