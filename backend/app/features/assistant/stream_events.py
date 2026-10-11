"""Own the provider event producer, heartbeat forwarding and upstream cleanup.

The bounded queue applies backpressure. Closing the consumer aborts the upstream
while its client is open, then cancels and drains the producer before returning.
No SQL Session or model-provider configuration crosses this boundary.
"""
import asyncio
import time
from contextlib import aclosing
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anyio
from loguru import logger

from app.features.assistant.event_protocol import AssistantEvent, heartbeat_event


async def engine_events_with_heartbeats(
    engine_iterator: AsyncIterator[AssistantEvent],
    run_id: int,
    interval_seconds: float = 15.0,
    *,
    on_idle: Callable[[], Awaitable[Any]] | None = None,
    on_abort: Callable[[], Awaitable[Any]] | None = None,
    poll_seconds: float = 1.0,
) -> AsyncIterator[AssistantEvent]:
    """Forward provider events while keeping reverse proxies' idle timers warm."""

    queue: asyncio.Queue[tuple[str, AssistantEvent | BaseException | None]] = asyncio.Queue(maxsize=1)

    async def produce() -> None:
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

    producer = asyncio.create_task(produce())
    heartbeat_at = time.monotonic() + interval_seconds
    interrupted = False
    try:
        while True:
            try:
                remaining = max(0, heartbeat_at - time.monotonic())
                timeout = min(remaining, poll_seconds) if on_idle is not None else remaining
                kind, value = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if on_idle is not None:
                    await on_idle()
                if time.monotonic() >= heartbeat_at:
                    yield heartbeat_event(run_id)
                    heartbeat_at = time.monotonic() + interval_seconds
                continue
            heartbeat_at = time.monotonic() + interval_seconds
            if kind == "event":
                assert isinstance(value, AssistantEvent)
                yield value
            elif kind == "error":
                assert isinstance(value, BaseException)
                raise value
            else:
                break
    except BaseException:
        interrupted = True
        raise
    finally:
        async def cleanup():
            # The producer owns the provider client. Abort while that client
            # is still open, before cancelling/draining its generator.
            if interrupted and on_abort is not None:
                try:
                    await on_abort()
                except (Exception, asyncio.CancelledError) as exc:
                    logger.error("Assistant upstream abort failed (error_type={})", type(exc).__name__)
            if not producer.done():
                producer.cancel()
            try:
                await producer
            except BaseException:
                pass
        pending_cleanup = asyncio.create_task(cleanup())
        cancelled = False
        with anyio.CancelScope(shield=True):
            while not pending_cleanup.done():
                try:
                    await asyncio.shield(pending_cleanup)
                except asyncio.CancelledError:
                    cancelled = True
        pending_cleanup.result()
        if cancelled and not interrupted:
            raise asyncio.CancelledError()


