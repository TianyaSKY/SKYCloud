"""数据库与 Redis 连接池及超时强类型配置."""

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_DB_POOL_SIZE: Final[int] = 10
DEFAULT_DB_MAX_OVERFLOW: Final[int] = 20
DEFAULT_DB_POOL_RECYCLE: Final[int] = 1800
DEFAULT_DB_POOL_TIMEOUT: Final[int] = 30
DEFAULT_DB_CONNECT_TIMEOUT: Final[int] = 5

DEFAULT_REDIS_CONNECT_TIMEOUT: Final[float] = 3.0
DEFAULT_REDIS_SOCKET_TIMEOUT: Final[float] = 5.0


def _read_int(name: str, default: int, *, min_value: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        return default
    else:
        return val if val >= min_value else default


def _read_float(name: str, default: float, *, min_value: float = 0.1) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
    except ValueError:
        return default
    else:
        return val if val >= min_value else default


@dataclass(frozen=True, slots=True)
class DatabasePoolConfig:
    """SQLAlchemy 数据库连接池配置."""

    pool_size: int = DEFAULT_DB_POOL_SIZE
    max_overflow: int = DEFAULT_DB_MAX_OVERFLOW
    pool_recycle: int = DEFAULT_DB_POOL_RECYCLE
    pool_timeout: int = DEFAULT_DB_POOL_TIMEOUT
    connect_timeout: int = DEFAULT_DB_CONNECT_TIMEOUT

    def __post_init__(self) -> None:
        """校验数据库连接池边界约束."""
        if self.pool_size < 1:
            msg = "pool_size must be >= 1"
            raise ValueError(msg)
        if self.max_overflow < 0:
            msg = "max_overflow must be >= 0"
            raise ValueError(msg)
        if self.pool_recycle < 1:
            msg = "pool_recycle must be >= 1"
            raise ValueError(msg)
        if self.pool_timeout < 1:
            msg = "pool_timeout must be >= 1"
            raise ValueError(msg)
        if self.connect_timeout < 1:
            msg = "connect_timeout must be >= 1"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RedisPoolConfig:
    """Redis 连接超时配置."""

    connect_timeout: float = DEFAULT_REDIS_CONNECT_TIMEOUT
    socket_timeout: float = DEFAULT_REDIS_SOCKET_TIMEOUT

    def __post_init__(self) -> None:
        """校验 Redis 连接超时边界约束."""
        if self.connect_timeout <= 0:
            msg = "connect_timeout must be > 0"
            raise ValueError(msg)
        if self.socket_timeout <= 0:
            msg = "socket_timeout must be > 0"
            raise ValueError(msg)


def get_database_pool_config() -> DatabasePoolConfig:
    """从环境变量解析或使用默认数据库连接池配置."""
    return DatabasePoolConfig(
        pool_size=_read_int("DB_POOL_SIZE", DEFAULT_DB_POOL_SIZE),
        max_overflow=_read_int(
            "DB_MAX_OVERFLOW", DEFAULT_DB_MAX_OVERFLOW, min_value=0,
        ),
        pool_recycle=_read_int("DB_POOL_RECYCLE", DEFAULT_DB_POOL_RECYCLE),
        pool_timeout=_read_int("DB_POOL_TIMEOUT", DEFAULT_DB_POOL_TIMEOUT),
        connect_timeout=_read_int("DB_CONNECT_TIMEOUT", DEFAULT_DB_CONNECT_TIMEOUT),
    )


def get_redis_pool_config() -> RedisPoolConfig:
    """从环境变量解析或使用默认 Redis 连接配置."""
    return RedisPoolConfig(
        connect_timeout=_read_float(
            "REDIS_CONNECT_TIMEOUT", DEFAULT_REDIS_CONNECT_TIMEOUT,
        ),
        socket_timeout=_read_float(
            "REDIS_SOCKET_TIMEOUT", DEFAULT_REDIS_SOCKET_TIMEOUT,
        ),
    )
