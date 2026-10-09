"""Keep synchronous worker lifetime inside the async operation that owns it."""
import asyncio
import threading
from app.infra.async_stream import drain_cleanup


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
