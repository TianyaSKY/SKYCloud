"""Own upstream HTTP resources through ASGI response delivery and teardown."""

import asyncio
from collections.abc import AsyncIterable, Mapping
from typing import Protocol, TypedDict, Unpack, cast

import anyio
from loguru import logger
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.infra.async_stream import CloseableAsyncIterator, close_reader, drain_cleanup


class AsyncCloseable(Protocol):
    """Describe an asynchronously closeable upstream HTTP resource."""

    async def aclose(self) -> None:
        """Release the resource's connection and transport state."""
        ...


class ResponseOptions(TypedDict, total=False):
    """Carry the supported Starlette response constructor options."""

    status_code: int
    headers: Mapping[str, str]
    media_type: str
    background: BackgroundTask


class UpstreamLease:
    """Own a client and optional response until shielded cleanup completes."""

    def __init__(self, client: AsyncCloseable) -> None:
        """Register the client before an upstream response is available."""
        self.client = client
        self.upstream: AsyncCloseable | None = None
        self.cleanup: asyncio.Task[None] | None = None

    async def close(self) -> None:
        """Close response then client once, draining cleanup before cancellation."""
        async def release() -> None:
            for resource, phase in (
                (self.upstream, "response"),
                (self.client, "client"),
            ):
                if resource is None:
                    continue
                try:
                    await resource.aclose()
                except (Exception, asyncio.CancelledError) as exc:
                    logger.error(
                        "Upstream HTTP {} close failed (error_type={})",
                        phase,
                        type(exc).__name__,
                    )

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
            raise asyncio.CancelledError


class UpstreamResponse(StreamingResponse):
    """Keep upstream resources alive through response delivery and teardown."""

    def __init__(
        self,
        content: AsyncIterable[bytes],
        *,
        lease: UpstreamLease,
        **kwargs: Unpack[ResponseOptions],
    ) -> None:
        """Bind a streaming body and its cleanup lease to the ASGI response."""
        super().__init__(content, **kwargs)
        self.lease = lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Deliver the response, then drain reader and lease cleanup in order."""
        failed = False
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            failed = True
            raise
        finally:
            async def release() -> None:
                await close_reader(
                    None,
                    cast("CloseableAsyncIterator[bytes]", self.body_iterator),
                )
                await self.lease.close()

            cancelled = await drain_cleanup(release())
            if cancelled and not failed:
                raise asyncio.CancelledError
