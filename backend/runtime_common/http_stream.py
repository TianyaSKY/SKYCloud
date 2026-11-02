"""具备取消安全特性的所属异步迭代器及其活动读取 teardown 机制."""

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
    """将已知异步生成器迭代器映射为其可关闭接口."""
    return cast("AsyncClosableIterator[T]", iterator)


async def close_reader(
    pending: asyncio.Task[T] | None,
    iterator: AsyncClosableIterator[T],
) -> bool:
    """在关闭迭代器前排空所属读取任务, 即使发生取消也能保证安全释放."""

    async def release() -> None:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        error: BaseException | None = None
        try:
            await iterator.aclose()
        except (Exception, asyncio.CancelledError) as exc:
            # 任何迭代器拆卸失败在此隔离: 流已结束,
            # 只有其自身关闭可能引发失败。
            error = exc
        if error is not None:
            logger.error(
                "Stream iterator close failed (error_type=%s)", type(error).__name__,
            )

    return await drain_cleanup(release())


async def drain_cleanup(operation: Coroutine[Any, Any, Any]) -> bool:
    """等待持有的拆卸逻辑完成, 当调用方被取消时不会取消该拆卸任务."""
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
    """能够异步释放底层传输通道的上游资源接口."""

    async def aclose(self) -> None:
        """异步关闭资源."""
        ...


class UpstreamLease:
    """具有所有权且能在取消后安全完成清理的上游响应与客户端租约."""

    def __init__(self, client: AsyncCloseable) -> None:
        """持有上游客户端; 中继层稍后附加对应的响应对象."""
        self.client = client
        self.upstream: AsyncCloseable | None = None
        self.cleanup: asyncio.Task[None] | None = None

    async def close(self) -> None:
        """确保上游响应和客户端仅被释放一次."""
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
                # 关闭上游 SDK 对象是租约清理的最后一步;
                # 保持另一资源无论此处抛出何种异常均可关闭。
                error = exc
            if error is not None:
                logger.error(
                    "Upstream HTTP %s close failed (error_type=%s)",
                    phase,
                    type(error).__name__,
                )


class ResponseOptions(TypedDict, total=False):
    """上游中继实际使用的流式响应配置选项."""

    status_code: int
    headers: Mapping[str, str] | None
    media_type: str | None
    background: BackgroundTask | None


class UpstreamResponse(StreamingResponse):
    """持有并负责关闭其上游租约的流式中继响应."""

    def __init__(
        self,
        content: AsyncIterable[str | bytes],
        *,
        lease: UpstreamLease,
        **options: Unpack[ResponseOptions],
    ) -> None:
        """附加所持有的租约以及由本响应负责关闭的迭代器."""
        super().__init__(content, **options)
        self.lease = lease
        self.iterator = cast("AsyncClosableIterator[str | bytes]", self.body_iterator)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """发送中继数据流并始终释放上游租约."""
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
    """等待一个有界时间间隔以获取数据分块, 并重新校验调用方身份凭证."""
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
    """即使读取未产出任何字节, 也强制执行总生命周期超时约束."""
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
