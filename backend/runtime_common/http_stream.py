"""Cancellation-safe teardown of an owned async iterator and its active read."""

import asyncio
import logging
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
)
from time import monotonic
from typing import Any, Final, Protocol, TypedDict, TypeVar, Unpack, cast

import anyio
from fastapi import HTTPException
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from runtime_common.async_stream import AsyncClosableIterator

T = TypeVar("T")

logger = logging.getLogger(__name__)

TIMEOUT_STATUS: Final = 502
TIMEOUT_DETAIL: Final = "Runtime response timed out"
DEFAULT_INTERVAL: Final = 1.0




def closable(iterator: AsyncIterator[T]) -> AsyncClosableIterator[T]:
    """Project a known async-generator iterator onto its closable surface."""
    return cast("AsyncClosableIterator[T]", iterator)


async def close_reader(
    pending: asyncio.Task[T] | None,
    iterator: AsyncClosableIterator[T],
) -> bool:
    """Drain the owned read before closing its iterator, despite cancellation."""

    async def release() -> None:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        error: BaseException | None = None
        try:
            await iterator.aclose()
        except (Exception, asyncio.CancelledError) as exc:
            # Any iterator teardown failure is isolated here: the stream is
            # already finished and only its own close can fail this call.
            error = exc
        if error is not None:
            logger.error(
                "Stream iterator close failed (error_type=%s)", type(error).__name__,
            )

    return await drain_cleanup(release())


async def drain_cleanup(operation: Coroutine[Any, Any, Any]) -> bool:
    """Wait for owned teardown without cancelling it when its caller is cancelled."""
    cancelled = False
    with anyio.CancelScope(shield=True):
        released = asyncio.create_task(operation)
        while not released.done():
            try:
                await asyncio.shield(released)
            except asyncio.CancelledError:
                cancelled = True
        released.result()
    return cancelled


class AsyncCloseable(Protocol):
    """An upstream resource that can release its transport asynchronously."""

    async def aclose(self) -> None:
        """Close the resource."""
        ...


class UpstreamLease:
    """Owned upstream response and client whose teardown survives cancellation."""

    def __init__(self, client: AsyncCloseable) -> None:
        """Own one upstream client; the relay attaches its response later."""
        self.client = client
        self.upstream: AsyncCloseable | None = None
        self.cleanup: asyncio.Task[None] | None = None

    async def close(self) -> None:
        """Release the upstream response and client exactly once."""
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._release())
        cancelled = False
        with anyio.CancelScope(shield=True):
            while not self.cleanup.done():
                try:
                    await asyncio.shield(self.cleanup)
                except asyncio.CancelledError:
                    cancelled = True
        self.cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _release(self) -> None:
        for resource, phase in ((self.upstream, "response"), (self.client, "client")):
            if resource is None:
                continue
            error: BaseException | None = None
            try:
                await resource.aclose()
            except (Exception, asyncio.CancelledError) as exc:
                # Closing an upstream SDK object is the last step of the lease;
                # keep the other resource closeable whatever this one raises.
                error = exc
            if error is not None:
                logger.error(
                    "Upstream HTTP %s close failed (error_type=%s)",
                    phase,
                    type(error).__name__,
                )


class ResponseOptions(TypedDict, total=False):
    """Streaming response options actually used by the upstream relay."""

    status_code: int
    headers: Mapping[str, str] | None
    media_type: str | None
    background: BackgroundTask | None


class UpstreamResponse(StreamingResponse):
    """Streaming relay response that owns and closes its upstream lease."""

    def __init__(
        self,
        content: AsyncIterable[str | bytes],
        *,
        lease: UpstreamLease,
        **options: Unpack[ResponseOptions],
    ) -> None:
        """Attach the owned lease and the closed-by-this-response iterator."""
        super().__init__(content, **options)
        self.lease = lease
        self.iterator = cast("AsyncClosableIterator[str | bytes]", self.body_iterator)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the relayed stream and always release the upstream lease."""
        failed = False
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            failed = True
            raise
        finally:

            async def release() -> None:
                await close_reader(None, self.iterator)
                await self.lease.close()

            cancelled = await drain_cleanup(release())
            if cancelled and not failed:
                raise asyncio.CancelledError


def _raise_if_expired(deadline: float) -> None:
    if monotonic() >= deadline:
        raise HTTPException(TIMEOUT_STATUS, TIMEOUT_DETAIL)


async def _wait_ready(
    pending: "asyncio.Task[T]",
    deadline: float,
    check: Callable[[], Awaitable[None]] | None,
    interval: float,
) -> bool:
    """Wait one bounded interval for a chunk and re-check the caller identity."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise HTTPException(TIMEOUT_STATUS, TIMEOUT_DETAIL)
    ready, _ = await asyncio.wait(
        {pending},
        timeout=remaining if check is None else min(interval, remaining),
    )
    _raise_if_expired(deadline)
    if check is not None:
        await check()
    _raise_if_expired(deadline)
    return bool(ready)


async def deadline_reader(
    iterator: AsyncIterator[T],
    seconds: float,
    *,
    check: Callable[[], Awaitable[None]] | None = None,
    interval: float = DEFAULT_INTERVAL,
) -> AsyncGenerator[T, None]:
    """Enforce a total lifetime even when a read produces no bytes."""
    owned = closable(iterator.__aiter__())
    deadline = monotonic() + seconds
    pending: asyncio.Task[T] | None = None
    failed = False

    async def read_next() -> T:
        return await anext(owned)
    try:
        while True:
            _raise_if_expired(deadline)
            if pending is None:
                pending = asyncio.create_task(read_next())
            if not await _wait_ready(pending, deadline, check, interval):
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield chunk
    except BaseException:
        failed = True
        raise
    finally:
        cancelled = await close_reader(pending, owned)
        if cancelled and not failed:
            raise asyncio.CancelledError
