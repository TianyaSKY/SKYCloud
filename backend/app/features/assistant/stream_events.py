"""持有提供商事件生产者、心跳转发与上游清理.

有界队列提供反压机制. 关闭消费者会在其客户端打开时中止上游,
随后在返回前取消并排干生产者.
任何 SQL Session 或模型提供商配置均不跨越此边界.
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
    """单个提供商事件流的有界空闲策略."""

    interval_seconds: float = 15.0
    on_idle: Callable[[], Awaitable[object]] | None = None
    on_abort: Callable[[], Awaitable[object]] | None = None
    poll_seconds: float = 1.0


QueueItem: TypeAlias = tuple[str, AssistantEvent | BaseException | None]
HeartbeatQueue: TypeAlias = "asyncio.Queue[QueueItem]"


def _raise_invariant(message: str) -> NoReturn:
    """抛出内部不变量错误, 避免类型检查抛出点."""
    raise AssertionError(message)


def _queued_event(value: AssistantEvent | BaseException | None) -> AssistantEvent:
    """收窄必须为已转发提供商事件的队列负载类型."""
    if not isinstance(value, AssistantEvent):
        _raise_invariant("heartbeat queue lost its event payload")
    return value


def _raise_queued_error(value: AssistantEvent | BaseException | None) -> NoReturn:
    """重新抛出生产者任务捕获的提供商异常."""
    if not isinstance(value, BaseException):
        _raise_invariant("heartbeat queue lost its error payload")
    raise value


async def _produce(
    engine_iterator: AsyncGenerator[AssistantEvent, None],
    queue: HeartbeatQueue,
) -> None:
    """将提供商迭代器排干至有界心跳队列中."""
    try:
        async with aclosing(engine_iterator):
            async for event in engine_iterator:
                await queue.put(("event", event))
    except asyncio.CancelledError:
        # 消费者已离开; 清理期间不在已满队列上阻塞.
        raise
    except BaseException as exc:  # 转发至下方的消费协程
        await queue.put(("error", exc))
    else:
        await queue.put(("done", None))


async def _consume(
    queue: HeartbeatQueue,
    run_id: int,
    options: HeartbeatOptions,
) -> AsyncGenerator[AssistantEvent, None]:
    """转发队列事件并在提供商空闲时产出心跳."""
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
    """优先中止存活提供商, 随后取消并排干生产者."""
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
    """转发提供商事件, 同时保持反向代理空闲计时器活跃."""
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
