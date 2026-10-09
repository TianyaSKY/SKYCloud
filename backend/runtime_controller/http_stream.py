"""Cancellation-safe teardown of an owned async iterator and its active read."""
import asyncio
import anyio
import logging


async def close_reader(pending, iterator):
    """Drain the owned read before closing its iterator, despite repeat cancellation."""
    async def release():
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await iterator.aclose()
        except (Exception, asyncio.CancelledError) as exc:
            logging.error("Stream iterator close failed (error_type=%s)", type(exc).__name__)
    return await drain_cleanup(release())


async def drain_cleanup(operation):
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


from starlette.responses import StreamingResponse


class UpstreamLease:
    def __init__(self, client):
        self.client = client
        self.upstream = None
        self.cleanup = None

    async def close(self):
        async def release():
            for resource, phase in ((self.upstream, 'response'), (self.client, 'client')):
                if resource is None:
                    continue
                try:
                    await resource.aclose()
                except (Exception, asyncio.CancelledError) as exc:
                    logging.error("Upstream HTTP %s close failed (error_type=%s)", phase, type(exc).__name__)
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(release())
        cancelled = False
        with anyio.CancelScope(shield=True):
            while not self.cleanup.done():
                try:
                    await asyncio.shield(self.cleanup)
                except asyncio.CancelledError:
                    cancelled = True
        self.cleanup.result()
        if cancelled:
            raise asyncio.CancelledError()


class UpstreamResponse(StreamingResponse):
    def __init__(self, content, *, lease, **kwargs):
        super().__init__(content, **kwargs)
        self.lease = lease

    async def __call__(self, scope, receive, send):
        failed = False
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            failed = True
            raise
        finally:
            async def release():
                await close_reader(None, self.body_iterator)
                await self.lease.close()
            cancelled = await drain_cleanup(release())
            if cancelled and not failed:
                raise asyncio.CancelledError()
