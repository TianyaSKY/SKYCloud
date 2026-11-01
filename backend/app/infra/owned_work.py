"""Keep synchronous worker lifetime inside the async operation that owns it."""

import asyncio
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from enum import Enum
from typing import ParamSpec, TypeVar, cast

from runtime_common.http_stream import drain_cleanup

_Event = TypeVar("_Event")
_P = ParamSpec("_P")
_R = TypeVar("_R")


class _OwnedWorkPolicy(Enum):
    SKIP_QUEUED = "skip_queued"
    PERSIST_QUEUED = "persist_queued"


def closeable_event_stream(
    events: AsyncIterator[_Event],
) -> AsyncGenerator[_Event, None]:
    """Re-type a provider event stream that is an async generator at runtime.

    Third-party event APIs (for example LangChain's ``astream_events``) declare
    ``AsyncIterator`` although they return an async generator. Stream owners must
    be able to ``aclose`` them so the generator is closed on disconnect.
    """
    return cast("AsyncGenerator[_Event, None]", events)


async def run_owned(
    function: Callable[_P, _R], /, *args: _P.args, **kwargs: _P.kwargs,
) -> _R:
    """Drain started work and skip work still queued when cancelled."""
    return await _run_owned(_OwnedWorkPolicy.SKIP_QUEUED, function, *args, **kwargs)


async def run_owned_persistent(
    function: Callable[_P, _R], /, *args: _P.args, **kwargs: _P.kwargs,
) -> _R:
    """Drain started work while persisting queued accounting or audit effects."""
    return await _run_owned(_OwnedWorkPolicy.PERSIST_QUEUED, function, *args, **kwargs)


async def _run_owned(
    policy: _OwnedWorkPolicy,
    function: Callable[_P, _R],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _R:
    lock = threading.Lock()
    cancelled = False

    def work() -> _R | None:
        with lock:
            if cancelled and policy is _OwnedWorkPolicy.SKIP_QUEUED:
                return None
        return function(*args, **kwargs)

    pending: asyncio.Task[_R | None] = asyncio.create_task(asyncio.to_thread(work))
    try:
        # The None branch is only reachable after cancellation, which is handled
        # below, so a normal shield result is the callable's actual value.
        return cast("_R", await asyncio.shield(pending))
    except asyncio.CancelledError:
        with lock:
            cancelled = True

        async def drain() -> bool:
            await asyncio.gather(pending, return_exceptions=True)
            return False

        await drain_cleanup(drain())
        raise
