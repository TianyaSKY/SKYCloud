"""带有时区感知的 UTC 时间工具, 显式转换遗留北京时间输入与展示."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    """返回当前带有时区感知的 UTC 时间点."""
    return datetime.now(UTC)


def to_utc_datetime(
    value: datetime | None,
    *,
    naive_is_utc: bool = False,
) -> datetime | None:
    """将遗留 naive 时间输入视为北京时间, 除非显式指定为 UTC."""
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
    """将时间序列化为显式 UTC 格式; 遗留 naive 数据库时间视为北京时间."""
    value = to_utc_datetime(value)
    return value.isoformat() if value is not None else None


def parse_legacy_datetime(value: str | None) -> datetime | None:
    """将缓存中的旧 naive 或带偏移量的时间字符串解析为 UTC 时间点."""
    return to_utc_datetime(datetime.fromisoformat(value)) if value else None
