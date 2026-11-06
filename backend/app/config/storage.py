"""MinIO 对象存储有界 I/O 与连接参数强类型配置."""

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_MINIO_CONNECT_TIMEOUT: Final[float] = 5.0
DEFAULT_MINIO_READ_TIMEOUT: Final[float] = 120.0
DEFAULT_MINIO_RETRIES: Final[int] = 2
DEFAULT_MINIO_ENDPOINT: Final[str] = "127.0.0.1:9000"
DEFAULT_MINIO_BUCKET: Final[str] = "skycloud"
DEFAULT_MINIO_SECURE: Final[bool] = False


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
    except ValueError:
        return default
    else:
        return val if val > 0 else default


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        return default
    else:
        return val if val >= 0 else default


def _read_bool(name: str, *, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class MinioConfig:
    """MinIO 客户端连接、重试与网络超时参数."""

    connect_timeout: float = DEFAULT_MINIO_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_MINIO_READ_TIMEOUT
    retries: int = DEFAULT_MINIO_RETRIES
    endpoint: str = DEFAULT_MINIO_ENDPOINT
    bucket: str = DEFAULT_MINIO_BUCKET
    secure: bool = DEFAULT_MINIO_SECURE

    def __post_init__(self) -> None:
        """校验 MinIO 存储配置约束."""
        if self.connect_timeout <= 0:
            msg = "connect_timeout must be > 0"
            raise ValueError(msg)
        if self.read_timeout <= 0:
            msg = "read_timeout must be > 0"
            raise ValueError(msg)
        if self.retries < 0:
            msg = "retries must be >= 0"
            raise ValueError(msg)
        if not self.endpoint.strip():
            msg = "endpoint cannot be empty"
            raise ValueError(msg)
        if not self.bucket.strip():
            msg = "bucket cannot be empty"
            raise ValueError(msg)


def get_minio_config() -> MinioConfig:
    """从环境变量解析或使用默认 MinIO 配置."""
    raw_endpoint = os.getenv("MINIO_ENDPOINT", DEFAULT_MINIO_ENDPOINT)
    endpoint = (
        raw_endpoint.strip()
        if raw_endpoint and raw_endpoint.strip()
        else DEFAULT_MINIO_ENDPOINT
    )
    raw_bucket = os.getenv("MINIO_BUCKET", DEFAULT_MINIO_BUCKET)
    bucket = (
        raw_bucket.strip()
        if raw_bucket and raw_bucket.strip()
        else DEFAULT_MINIO_BUCKET
    )
    return MinioConfig(
        connect_timeout=_read_float(
            "MINIO_CONNECT_TIMEOUT", DEFAULT_MINIO_CONNECT_TIMEOUT,
        ),
        read_timeout=_read_float(
            "MINIO_READ_TIMEOUT", DEFAULT_MINIO_READ_TIMEOUT,
        ),
        retries=_read_int("MINIO_RETRIES", DEFAULT_MINIO_RETRIES),
        endpoint=endpoint,
        bucket=bucket,
        secure=_read_bool("MINIO_SECURE", default=DEFAULT_MINIO_SECURE),
    )
