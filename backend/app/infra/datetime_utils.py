"""北京时区时间工具：DB 列多为无时区 naive datetime，统一按 Asia/Shanghai 处理。"""

from datetime import datetime
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
    """序列化为 ISO 字符串前先规范到北京 naive。"""
    value = to_beijing_naive(value)
    return value.isoformat() if value else None
