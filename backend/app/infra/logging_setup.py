"""集中式 Loguru 配置: API / worker / MCP 三个进程共用.

标准库 logging 记录(uvicorn、sqlalchemy、httpx 等第三方库)经 InterceptHandler
转发进 Loguru, 实现全站统一输出.
"""

import logging
import os
import sys
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from types import FrameType


class InterceptHandler(logging.Handler):
    """把标准库 logging 记录转发到 Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """转发单条日志记录, 脱敏异常信息与堆栈追踪链."""
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        if record.exc_info and record.exc_info[1] is not None:
            # 第三方库的堆栈链可能包含 SQL 绑定参数、供应商请求体
            # 和请求敏感数据, 即使已禁用 diagnose/backtrace。
            logger.opt(depth=depth).log(
                level,
                "Library failure in {} (error_type={})",
                record.name,
                type(record.exc_info[1]).__name__,
            )
        else:
            logger.opt(depth=depth).log(level, record.getMessage())


def setup_logging() -> None:
    """幂等: 移除默认 handler, 装 stderr handler, 并把 stdlib 日志路由到 Loguru."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=os.getenv("LOG_LEVEL", "INFO"),
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
            "{name}:{function}:{line} - {message}"
        ),
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        _l = logging.getLogger(name)
        _l.handlers = [InterceptHandler()]
        _l.propagate = False
