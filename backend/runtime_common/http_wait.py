"""带有持续凭据校验和排空清理机制的有界准入等待."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final, TypeVar

import anyio
from fastapi import HTTPException

from runtime_common.http_stream import drain_cleanup

R = TypeVar("R")

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL: Final = 1.0


@dataclass(frozen=True)
class WaitPolicy:
    """有界准入等待策略: 截止时间、身份检查与中止钩子."""

    check: Callable[[], Awaitable[None]]
    seconds: float
    status: int
    detail: str
    interval: float = DEFAULT_INTERVAL
    on_abort: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True)
class WaitCleanup:
    """放弃准入等待时由调用方拥有的清理策略."""

    cleanup_drain: Callable[[Coroutine[Any, Any, Any]], Awaitable[bool]]
    report_abort_error: Callable[[BaseException], None] | None = None


def _own(operation: Awaitable[R]) -> asyncio.Task[R]:
    """将等待的操作包装为一个可取消的单一任务."""
    return asyncio.ensure_future(operation)


def _raise_timeout(policy: WaitPolicy) -> None:
    raise HTTPException(policy.status, policy.detail)


async def _abort(policy: WaitPolicy, cleanup: WaitCleanup) -> None:
    """执行被放弃操作的中止钩子, 并隔离其抛出的异常."""
    on_abort = policy.on_abort
    if on_abort is None:
        return
    error: BaseException | None = None
    try:
        await on_abort()
    except (Exception, asyncio.CancelledError) as exc:
        # 该操作已被放弃; 此处仅其自身的中止钩子可能引发异常。
        error = exc
    if error is None:
        return
    if cleanup.report_abort_error is not None:
        cleanup.report_abort_error(error)
    else:
        logger.error(
            "HTTP admission cleanup failed (error_type=%s)", type(error).__name__,
        )


async def _settle(policy: WaitPolicy, cleanup: WaitCleanup, *, cancelled: bool) -> None:
    """传递受保护清理已观测到的取消信号."""
    if cancelled:
        await cleanup.cleanup_drain(_abort(policy, cleanup))
        raise asyncio.CancelledError
    try:
        # 外部 AnyIO 作用域可能已被取消,
        # 但在受保护清理外部尚未遇到检查点来交付取消信号。
        await anyio.lowlevel.checkpoint_if_cancelled()
    except asyncio.CancelledError:
        await cleanup.cleanup_drain(_abort(policy, cleanup))
        raise


async def guarded_wait(
    operation: Awaitable[R],
    policy: WaitPolicy,
    *,
    cleanup: WaitCleanup | None = None,
) -> R:
    """等待单次有界准入操作, 在等待过程中持续复核调用者身份."""
    active = cleanup if cleanup is not None else WaitCleanup(drain_cleanup)
    pending = _own(operation)
    deadline = time.monotonic() + policy.seconds
    interrupted = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _raise_timeout(policy)
            ready, _ = await asyncio.wait(
                {pending},
                timeout=min(policy.interval, remaining),
            )
            await policy.check()
            if time.monotonic() >= deadline:
                _raise_timeout(policy)
            if ready:
                return pending.result()
    except BaseException:
        interrupted = True
        raise
    finally:

        async def cancel_pending() -> None:
            if not pending.done():
                pending.cancel()
            try:
                await asyncio.gather(pending, return_exceptions=True)
            finally:
                if interrupted:
                    await _abort(policy, active)

        cancelled = await active.cleanup_drain(cancel_pending())
        if not interrupted:
            await _settle(policy, active, cancelled=cancelled)
