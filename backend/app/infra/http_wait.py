"""Shared admission wait with backend cleanup ownership and diagnostics."""

from collections.abc import Awaitable
from typing import TypeVar

from loguru import logger
from runtime_controller.http_wait import (
    WaitCleanup,
    WaitPolicy,
)
from runtime_controller.http_wait import (
    guarded_wait as _guarded_wait,
)

from app.infra.async_stream import drain_cleanup

R = TypeVar("R")


def _report(error: BaseException) -> None:
    """Log only the exception type so private upstream details stay private."""
    logger.error("HTTP admission cleanup failed (error_type={})", type(error).__name__)


async def guarded_wait(
    operation: Awaitable[R],
    policy: WaitPolicy,
    *,
    cleanup: WaitCleanup | None = None,
) -> R:
    """Wait for a bounded admission, with app-owned cleanup and logging."""
    active = cleanup if cleanup is not None else WaitCleanup(drain_cleanup, _report)
    return await _guarded_wait(operation, policy, cleanup=active)


__all__ = ["WaitCleanup", "WaitPolicy", "guarded_wait"]
