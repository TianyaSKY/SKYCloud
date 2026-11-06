"""上传大小、分片、并发与配额强类型配置."""

import os
from dataclasses import dataclass
from typing import Final

# 默认字节限额
DEFAULT_FILE_LIMIT: Final[int] = 256 * 1024 * 1024  # 256 MiB
DEFAULT_AVATAR_LIMIT: Final[int] = 5 * 1024 * 1024  # 5 MiB
DEFAULT_REQUEST_LIMIT: Final[int] = 512 * 1024 * 1024  # 512 MiB
DEFAULT_CHUNK_SIZE: Final[int] = 2 * 1024 * 1024  # 2 MiB
DEFAULT_MAX_CHUNK_SIZE: Final[int] = 64 * 1024 * 1024  # 64 MiB
DEFAULT_MAX_TOTAL_CHUNKS: Final[int] = 10000
DEFAULT_REQUEST_CONCURRENCY: Final[int] = 8

# 默认容量与频率配额
DEFAULT_USER_CAPACITY: Final[int] = 10 * 1024**3  # 10 GiB
DEFAULT_WORKSPACE_CAPACITY: Final[int] = 50 * 1024**3  # 50 GiB
DEFAULT_DAILY_BYTES: Final[int] = 5 * 1024**3  # 5 GiB
DEFAULT_DAILY_ATTEMPTS: Final[int] = 200
DEFAULT_USER_CONCURRENCY: Final[int] = 8
DEFAULT_WORKSPACE_CONCURRENCY: Final[int] = 32
DEFAULT_RESERVATION_TTL_SECONDS: Final[int] = 3600


def positive_limit(name: str, default: int) -> int:
    """从环境变量读取正整数限制值, 非法或小于等于 0 时抛出异常或回退."""
    message = f"{name} must be a positive integer"
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(message) from exc
    if value <= 0:
        raise RuntimeError(message)
    return value


@dataclass(frozen=True, slots=True)
class UploadLimitsConfig:
    """上传文件与请求边界参数."""

    max_file_size: int = DEFAULT_FILE_LIMIT
    max_avatar_size: int = DEFAULT_AVATAR_LIMIT
    max_request_size: int = DEFAULT_REQUEST_LIMIT
    chunk_size: int = DEFAULT_CHUNK_SIZE
    max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE
    max_total_chunks: int = DEFAULT_MAX_TOTAL_CHUNKS
    request_concurrency: int = DEFAULT_REQUEST_CONCURRENCY

    def __post_init__(self) -> None:
        """校验上传文件限制边界."""
        if self.max_file_size <= 0:
            msg = "max_file_size must be > 0"
            raise ValueError(msg)
        if self.max_avatar_size <= 0:
            msg = "max_avatar_size must be > 0"
            raise ValueError(msg)
        if self.max_request_size <= 0:
            msg = "max_request_size must be > 0"
            raise ValueError(msg)
        if self.chunk_size <= 0:
            msg = "chunk_size must be > 0"
            raise ValueError(msg)
        if self.max_chunk_size <= 0:
            msg = "max_chunk_size must be > 0"
            raise ValueError(msg)
        if self.max_total_chunks <= 0:
            msg = "max_total_chunks must be > 0"
            raise ValueError(msg)
        if self.request_concurrency <= 0:
            msg = "request_concurrency must be > 0"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class UploadQuotaConfig:
    """用户和工作区存储与准入配额."""

    user_capacity: int = DEFAULT_USER_CAPACITY
    workspace_capacity: int = DEFAULT_WORKSPACE_CAPACITY
    daily_bytes: int = DEFAULT_DAILY_BYTES
    daily_attempts: int = DEFAULT_DAILY_ATTEMPTS
    user_concurrency: int = DEFAULT_USER_CONCURRENCY
    workspace_concurrency: int = DEFAULT_WORKSPACE_CONCURRENCY
    reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS

    def __post_init__(self) -> None:
        """校验上传配额边界约束."""
        if self.user_capacity <= 0:
            msg = "user_capacity must be > 0"
            raise ValueError(msg)
        if self.workspace_capacity <= 0:
            msg = "workspace_capacity must be > 0"
            raise ValueError(msg)
        if self.daily_bytes <= 0:
            msg = "daily_bytes must be > 0"
            raise ValueError(msg)
        if self.daily_attempts <= 0:
            msg = "daily_attempts must be > 0"
            raise ValueError(msg)
        if self.user_concurrency <= 0:
            msg = "user_concurrency must be > 0"
            raise ValueError(msg)
        if self.workspace_concurrency <= 0:
            msg = "workspace_concurrency must be > 0"
            raise ValueError(msg)
        if self.reservation_ttl_seconds <= 0:
            msg = "reservation_ttl_seconds must be > 0"
            raise ValueError(msg)


def get_upload_limits_config() -> UploadLimitsConfig:
    """从环境变量解析或使用默认上传限制配置."""
    return UploadLimitsConfig(
        max_file_size=positive_limit(
            "UPLOAD_MAX_FILE_SIZE", DEFAULT_FILE_LIMIT,
        ),
        max_avatar_size=positive_limit(
            "UPLOAD_MAX_AVATAR_SIZE", DEFAULT_AVATAR_LIMIT,
        ),
        max_request_size=positive_limit(
            "UPLOAD_MAX_REQUEST_SIZE", DEFAULT_REQUEST_LIMIT,
        ),
        chunk_size=positive_limit("UPLOAD_CHUNK_SIZE", DEFAULT_CHUNK_SIZE),
        max_chunk_size=positive_limit(
            "UPLOAD_MAX_CHUNK_SIZE", DEFAULT_MAX_CHUNK_SIZE,
        ),
        max_total_chunks=positive_limit(
            "UPLOAD_MAX_TOTAL_CHUNKS", DEFAULT_MAX_TOTAL_CHUNKS,
        ),
        request_concurrency=positive_limit(
            "UPLOAD_REQUEST_CONCURRENCY", DEFAULT_REQUEST_CONCURRENCY,
        ),
    )


def get_upload_quota_config() -> UploadQuotaConfig:
    """从环境变量解析或使用默认上传配额配置."""
    return UploadQuotaConfig(
        user_capacity=positive_limit(
            "UPLOAD_USER_CAPACITY", DEFAULT_USER_CAPACITY,
        ),
        workspace_capacity=positive_limit(
            "UPLOAD_WORKSPACE_CAPACITY", DEFAULT_WORKSPACE_CAPACITY,
        ),
        daily_bytes=positive_limit(
            "UPLOAD_DAILY_BYTES", DEFAULT_DAILY_BYTES,
        ),
        daily_attempts=positive_limit(
            "UPLOAD_DAILY_ATTEMPTS", DEFAULT_DAILY_ATTEMPTS,
        ),
        user_concurrency=positive_limit(
            "UPLOAD_USER_CONCURRENCY", DEFAULT_USER_CONCURRENCY,
        ),
        workspace_concurrency=positive_limit(
            "UPLOAD_WORKSPACE_CONCURRENCY", DEFAULT_WORKSPACE_CONCURRENCY,
        ),
        reservation_ttl_seconds=positive_limit(
            "UPLOAD_RESERVATION_TTL_SECONDS", DEFAULT_RESERVATION_TTL_SECONDS,
        ),
    )
