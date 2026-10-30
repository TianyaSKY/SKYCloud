"""Own the provider event producer, heartbeat forwarding and upstream cleanup.

The bounded queue applies backpressure. Closing the consumer aborts the upstream
while its client is open, then cancels and drains the producer before returning.
No SQL Session or model-provider configuration crosses this boundary.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import NoReturn, TypeAlias

import anyio
from loguru import logger

from app.features.assistant.event_protocol import AssistantEvent, heartbeat_event


@dataclass(frozen=True, slots=True, kw_only=True)
class HeartbeatOptions:
    """Bounded idle policy for one provider event stream."""

    interval_seconds: float = 15.0
    on_idle: Callable[[], Awaitable[object]] | None = None
    on_abort: Callable[[], Awaitable[object]] | None = None
    poll_seconds: float = 1.0


QueueItem: TypeAlias = tuple[str, AssistantEvent | BaseException | None]
HeartbeatQueue: TypeAlias = "asyncio.Queue[QueueItem]"


def _raise_invariant(message: str) -> NoReturn:
    """Raise the internal invariant error without a type-check raise site."""
    raise AssertionError(message)


def _queued_event(value: AssistantEvent | BaseException | None) -> AssistantEvent:
    """Narrow a queue payload that must be a forwarded provider event."""
    if not isinstance(value, AssistantEvent):
        _raise_invariant("heartbeat queue lost its event payload")
    return value


def _raise_queued_error(value: AssistantEvent | BaseException | None) -> NoReturn:
    """Re-raise the provider exception captured by the producer task."""
    if not isinstance(value, BaseException):
        _raise_invariant("heartbeat queue lost its error payload")
    raise value


async def _produce(
    engine_iterator: AsyncGenerator[AssistantEvent, None],
    queue: HeartbeatQueue,
) -> None:
    """Drain the provider iterator into the bounded heartbeat queue."""
    try:
        async with aclosing(engine_iterator):
            async for event in engine_iterator:
                await queue.put(("event", event))
    except asyncio.CancelledError:
        # The consumer is gone; do not block on a full queue during cleanup.
        raise
    except BaseException as exc:  # forwarded to the consumer below
        await queue.put(("error", exc))
    else:
        await queue.put(("done", None))


async def _consume(
    queue: HeartbeatQueue,
    run_id: int,
    options: HeartbeatOptions,
) -> AsyncGenerator[AssistantEvent, None]:
    """Forward queued events and emit heartbeats while the provider is idle."""
    heartbeat_at = time.monotonic() + options.interval_seconds
    while True:
        try:
            remaining = max(0, heartbeat_at - time.monotonic())
            timeout = (
                min(remaining, options.poll_seconds)
                if options.on_idle is not None
                else remaining
            )
            kind, value = await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            if options.on_idle is not None:
                await options.on_idle()
            if time.monotonic() >= heartbeat_at:
                yield heartbeat_event(run_id)
                heartbeat_at = time.monotonic() + options.interval_seconds
            continue
        heartbeat_at = time.monotonic() + options.interval_seconds
        if kind == "event":
            yield _queued_event(value)
        elif kind == "error":
            _raise_queued_error(value)
        else:
            return


async def _cleanup_producer(
    producer: "asyncio.Task[None]",
    on_abort: Callable[[], Awaitable[object]] | None,
    *,
    interrupted: bool,
) -> None:
    """Abort the live provider first, then cancel and drain the producer."""
    if interrupted and on_abort is not None:
        try:
            await on_abort()
        except (Exception, asyncio.CancelledError) as exc:
            logger.error(
                "Assistant upstream abort failed (error_type={})",
                type(exc).__name__,
            )
    if not producer.done():
        producer.cancel()
    with suppress(BaseException):
        await producer


async def engine_events_with_heartbeats(
    engine_iterator: AsyncGenerator[AssistantEvent, None],
    run_id: int,
    options: HeartbeatOptions | None = None,
) -> AsyncGenerator[AssistantEvent, None]:
    """Forward provider events while keeping reverse proxies' idle timers warm."""
    policy = options or HeartbeatOptions()
    queue: HeartbeatQueue = asyncio.Queue(maxsize=1)
    producer = asyncio.create_task(_produce(engine_iterator, queue))
    interrupted = False
    try:
        async with aclosing(_consume(queue, run_id, policy)) as consumer:
            async for event in consumer:
                yield event
    except BaseException:
        interrupted = True
        raise
    finally:
        pending_cleanup = asyncio.create_task(
            _cleanup_producer(
                producer,
                policy.on_abort,
                interrupted=interrupted,
            ),
        )
        cancelled = False
        with anyio.CancelScope(shield=True):
            while not pending_cleanup.done():
                try:
                    await asyncio.shield(pending_cleanup)
                except asyncio.CancelledError:
                    cancelled = True
        pending_cleanup.result()
        if cancelled and not interrupted:
            raise asyncio.CancelledError
