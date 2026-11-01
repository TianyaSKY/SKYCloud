"""Owned Redis admission and token-checked release for expert runs."""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from loguru import logger
from runtime_common.http_stream import drain_cleanup

from app.exceptions import ConflictError, ServiceOperationError
from app.infra import extensions
from app.infra.extensions import RedisValue
from app.infra.owned_work import run_owned, run_owned_persistent

_LOCAL_RUNTIME_LOCKS: dict[int, asyncio.Lock] = {}
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

LeaseCallback = Callable[[str], Awaitable[None]]
LostCallback = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class _LeaseState:
    """Mutable ownership marker shared with the renewal worker."""

    key: str
    token: str
    owner: asyncio.Task[object]
    attempted: bool = False
    lost: bool = False


def _lease_key(runtime_id: int) -> str:
    """Return the Redis key that arbitrates one expert runtime."""
    return f"assistant:expert:runtime:{runtime_id}"


def _lease_ttl() -> int:
    """Return the validated Redis lease lifetime in seconds."""
    ttl = int(os.getenv("ASSISTANT_RUNTIME_LOCK_TTL", "3600"))
    if ttl <= 0:
        message = "Lease lifetime must be positive"
        raise ValueError(message)
    return ttl


def _acquire(
    state: _LeaseState,
    ttl_seconds: int,
    loop: asyncio.AbstractEventLoop,
) -> tuple[bool | None, float]:
    """Attempt the non-blocking Redis SET that grants runtime ownership."""
    state.attempted = True
    deadline = loop.time() + ttl_seconds
    result = extensions.redis_client.set(
        state.key,
        state.token,
        nx=True,
        ex=ttl_seconds,
    )
    return result, deadline


def _renew(
    state: _LeaseState,
    ttl_seconds: int,
    loop: asyncio.AbstractEventLoop,
) -> tuple[RedisValue, float]:
    """Extend the token only while Redis still holds our ownership."""
    deadline = loop.time() + ttl_seconds
    result = extensions.redis_client.eval(
        RENEW, 1, state.key, state.token, ttl_seconds * 1000,
    )
    return result, deadline


def _lose_ownership(state: _LeaseState) -> None:
    """Stop the owning task once the token can no longer be renewed."""
    state.lost = True
    state.owner.cancel("expert-runtime-lease-lost")


def _confirm_ownership(
    *,
    result: bool | None,
    deadline: float,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Reject an acquisition that Redis did not confirm in time."""
    if result is False or result is None:
        busy = "Expert runtime is already busy"
        raise ConflictError(busy)
    if loop.time() >= deadline:
        expired = "Expert runtime lock confirmation expired"
        raise ServiceOperationError(expired)


async def _keep_alive(
    state: _LeaseState,
    ttl_seconds: int,
    deadline: float,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Renew the Redis token until ownership is lost or the deadline expires."""
    pending: asyncio.Task[tuple[RedisValue, float]] | None = None
    try:
        while True:
            interval = min(ttl_seconds / 3, max(0, deadline - loop.time()))
            await asyncio.sleep(interval)
            remaining = deadline - loop.time()
            if remaining <= 0:
                _lose_ownership(state)
                return
            pending = asyncio.create_task(
                run_owned(_renew, state, ttl_seconds, loop),
            )
            ready, _ = await asyncio.wait({pending}, timeout=remaining)
            if not ready:
                # Signal loss before draining a slow Redis operation. A
                # delayed reply cannot keep the expert task executing.
                _lose_ownership(state)
                return
            result, next_deadline = pending.result()
            pending = None
            if not result or loop.time() >= deadline:
                _lose_ownership(state)
                return
            deadline = next_deadline
    except Exception as exc:
        logger.error(
            "Expert runtime lease renewal failed (error_type={})",
            type(exc).__name__,
        )
        _lose_ownership(state)
    finally:
        if pending is not None:
            stopping = pending
            stopping.cancel()

            async def drain() -> None:
                await asyncio.gather(stopping, return_exceptions=True)

            await drain_cleanup(drain())


def _release(state: _LeaseState) -> None:
    """Delete the token only when Redis still records our ownership."""
    try:
        # Also clean up an ambiguous SET reply: it may have succeeded on
        # Redis even if the client failed before receiving its response.
        extensions.redis_client.eval(RELEASE, 1, state.key, state.token)
    except Exception as exc:
        logger.error(
            "Expert runtime lease release failed (error_type={})",
            type(exc).__name__,
        )


async def _start_lease(
    state: _LeaseState,
    loop: asyncio.AbstractEventLoop,
) -> tuple[int, float]:
    """Acquire the Redis token, mapping every failure to a safe error."""
    try:
        ttl = _lease_ttl()
        result, deadline = await run_owned(_acquire, state, ttl, loop)
    except Exception as exc:
        logger.error(
            "Expert runtime lease acquisition failed (error_type={})",
            type(exc).__name__,
        )
        unavailable = "Expert runtime lock is temporarily unavailable"
        raise ServiceOperationError(unavailable) from None
    _confirm_ownership(result=result, deadline=deadline, loop=loop)
    return ttl, deadline


async def _finish_lease(
    state: _LeaseState,
    keeper: asyncio.Task[None] | None,
    on_lost: LostCallback | None,
    local_lock: asyncio.Lock,
) -> None:
    """Leave Redis, the renewal worker, and the local lock in their order."""
    try:
        if state.lost and on_lost is not None:
            try:
                async with asyncio.timeout(6):
                    await on_lost()
            except (Exception, asyncio.CancelledError) as exc:
                logger.error(
                    "Expert runtime lease abort failed (error_type={})",
                    type(exc).__name__,
                )
        if keeper is not None:
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)
        if state.attempted:
            await run_owned_persistent(_release, state)
    finally:
        local_lock.release()


@asynccontextmanager
async def runtime_lease(
    runtime_id: int,
    *,
    on_lost: LostCallback | None = None,
    before_acquire: LeaseCallback | None = None,
) -> AsyncGenerator[str, None]:
    """Admit one expert run to a runtime until its block finishes."""
    local_lock = _LOCAL_RUNTIME_LOCKS.setdefault(runtime_id, asyncio.Lock())
    if local_lock.locked():
        busy = "Expert runtime is already busy"
        raise ConflictError(busy)
    await local_lock.acquire()
    owner = asyncio.current_task()
    if owner is None:
        message = "Expert runtime lease requires a running task"
        raise ServiceOperationError(message)
    state = _LeaseState(
        key=_lease_key(runtime_id),
        token=uuid.uuid4().hex,
        owner=owner,
    )
    loop = asyncio.get_running_loop()
    keeper: asyncio.Task[None] | None = None
    failed = False

    try:
        if before_acquire is not None:
            await before_acquire(state.token)
        ttl, deadline = await _start_lease(state, loop)
        keeper = asyncio.create_task(_keep_alive(state, ttl, deadline, loop))
        yield state.token
    except asyncio.CancelledError:
        failed = True
        if state.lost:
            lost_message = "Expert runtime lock ownership was lost"
            raise ServiceOperationError(lost_message) from None
        raise
    except BaseException:
        failed = True
        raise
    finally:
        cleanup = _finish_lease(state, keeper, on_lost, local_lock)
        cancelled = await drain_cleanup(cleanup)
        if cancelled and not failed:
            raise asyncio.CancelledError
