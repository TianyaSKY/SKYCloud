"""Own upstream HTTP resources through ASGI response delivery and teardown."""
import asyncio
import anyio
from loguru import logger
from starlette.responses import StreamingResponse
from app.infra.async_stream import close_reader, drain_cleanup


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
                    logger.error("Upstream HTTP {} close failed (error_type={})", phase, type(exc).__name__)
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
