"""Bounded admission waits with ongoing credential checks and drained cleanup."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final, TypeVar

import anyio
from fastapi import HTTPException

from runtime_controller.http_stream import drain_cleanup

R = TypeVar("R")

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL: Final = 1.0


@dataclass(frozen=True)
class WaitPolicy:
    """Bounded admission wait: deadline, identity checks and abort hook."""

    check: Callable[[], Awaitable[None]]
    seconds: float
    status: int
    detail: str
    interval: float = DEFAULT_INTERVAL
    on_abort: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True)
class WaitCleanup:
    """Caller-owned cleanup policy for an abandoned admission wait."""

    cleanup_drain: Callable[[Coroutine[Any, Any, None]], Awaitable[bool]]
    report_abort_error: Callable[[BaseException], None] | None = None


def _own(operation: Awaitable[R]) -> asyncio.Task[R]:
    """Own the awaited operation as one cancellable task."""
    return asyncio.ensure_future(operation)


def _raise_timeout(policy: WaitPolicy) -> None:
    raise HTTPException(policy.status, policy.detail)


async def _abort(policy: WaitPolicy, cleanup: WaitCleanup) -> None:
    """Run the abandoned operation's abort hook, isolating its failure."""
    on_abort = policy.on_abort
    if on_abort is None:
        return
    error: BaseException | None = None
    try:
        await on_abort()
    except (Exception, asyncio.CancelledError) as exc:
        # The operation was already abandoned; only its own abort can fail here.
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
    """Deliver a cancellation that the shielded cleanup already observed."""
    if cancelled:
        await cleanup.cleanup_drain(_abort(policy, cleanup))
        raise asyncio.CancelledError
    try:
        # An outer AnyIO scope may already be cancelled, with no checkpoint
        # outside shielded cleanup to deliver it yet.
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
    """Await one bounded admission operation, re-checking identity as it waits."""
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
