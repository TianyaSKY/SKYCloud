"""后台 Worker 并发线程强类型配置."""

import os
from dataclasses import dataclass
from typing import Final

# 保持与旧 .env.example 同步的默认并发线程数 20
DEFAULT_WORKER_MAX_THREADS: Final[int] = 20
DEFAULT_WORKER_ORGANIZE_THREADS: Final[int] = 1
DEFAULT_DOCUMENT_CONVERSION_WORKERS: Final[int] = 2
DEFAULT_VIDEO_PROCESSING_WORKERS: Final[int] = 2


def _read_worker_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        return default
    else:
        return max(1, val)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """后台任务执行与转码并发限制."""

    max_threads: int = DEFAULT_WORKER_MAX_THREADS
    organize_threads: int = DEFAULT_WORKER_ORGANIZE_THREADS
    document_conversion_workers: int = DEFAULT_DOCUMENT_CONVERSION_WORKERS
    video_processing_workers: int = DEFAULT_VIDEO_PROCESSING_WORKERS

    def __post_init__(self) -> None:
        """校验 Worker 并发边界约束."""
        if self.max_threads < 1:
            msg = "max_threads must be >= 1"
            raise ValueError(msg)
        if self.organize_threads < 1:
            msg = "organize_threads must be >= 1"
            raise ValueError(msg)
        if self.document_conversion_workers < 1:
            msg = "document_conversion_workers must be >= 1"
            raise ValueError(msg)
        if self.video_processing_workers < 1:
            msg = "video_processing_workers must be >= 1"
            raise ValueError(msg)


def get_worker_config() -> WorkerConfig:
    """从环境变量解析或使用默认 Worker 并发配置."""
    return WorkerConfig(
        max_threads=_read_worker_int(
            "WORKER_MAX_THREADS", DEFAULT_WORKER_MAX_THREADS,
        ),
        organize_threads=_read_worker_int(
            "WORKER_ORGANIZE_THREADS", DEFAULT_WORKER_ORGANIZE_THREADS,
        ),
        document_conversion_workers=_read_worker_int(
            "WORKER_DOCUMENT_CONVERSION_WORKERS",
            DEFAULT_DOCUMENT_CONVERSION_WORKERS,
        ),
        video_processing_workers=_read_worker_int(
            "WORKER_VIDEO_PROCESSING_WORKERS",
            DEFAULT_VIDEO_PROCESSING_WORKERS,
        ),
    )
