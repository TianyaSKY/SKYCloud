"""Keep synchronous worker lifetime inside the async operation that owns it."""
import asyncio
import threading
from collections.abc import AsyncGenerator, AsyncIterator
from typing import TypeVar, cast

from app.infra.async_stream import drain_cleanup

_Event = TypeVar("_Event")


def closeable_event_stream(events: AsyncIterator[_Event]) -> AsyncGenerator[_Event, None]:
    """Re-type a provider event stream that is an async generator at runtime.

    Third-party event APIs (for example LangChain's ``astream_events``) declare
    ``AsyncIterator`` although they return an async generator. Stream owners must
    be able to ``aclose`` them so the generator is closed on disconnect.
    """
    return cast(AsyncGenerator[_Event, None], events)


async def run_owned(function, *args, cancel_queued=True, **kwargs):
    """Drain started work; optionally skip work still queued when cancelled.

    Accounting/audit calls use cancel_queued=False because their accepted
    effects must be persisted even if delivery of the result is cancelled.
    """
    lock = threading.Lock()
    cancelled = False
    def work():
        with lock:
            if cancelled and cancel_queued:
                return None
        return function(*args, **kwargs)
    pending = asyncio.create_task(asyncio.to_thread(work))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        with lock:
            cancelled = True
        async def drain():
            await asyncio.gather(pending, return_exceptions=True)
        await drain_cleanup(drain())
        raise
