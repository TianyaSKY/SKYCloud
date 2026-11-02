"""将同步 worker 的生命周期限制在其所属的异步操作之内."""

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
    """将运行时实际为异步生成器的供应商事件流重新声明类型.

    第三方事件 API (例如 LangChain 的 ``astream_events``) 虽返回异步生成器,
    但类型注解仅声明为 ``AsyncIterator``。流所有者必须能够
    对其调用 ``aclose``, 以便在连接断开时及时关闭生成器。
    """
    return cast("AsyncGenerator[_Event, None]", events)


async def run_owned(
    function: Callable[_P, _R], /, *args: _P.args, **kwargs: _P.kwargs,
) -> _R:
    """排空已启动的任务, 并在取消时跳过仍在队列中等待的任务."""
    return await _run_owned(_OwnedWorkPolicy.SKIP_QUEUED, function, *args, **kwargs)


async def run_owned_persistent(
    function: Callable[_P, _R], /, *args: _P.args, **kwargs: _P.kwargs,
) -> _R:
    """排空已启动的任务, 同时保留已入队的记账或审计副作用."""
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
        # None 分支仅在取消后可达 (由下方逻辑处理),
        # 因此正常的 shield 结果即为可调用对象的实际返回值。
        return cast("_R", await asyncio.shield(pending))
    except asyncio.CancelledError:
        with lock:
            cancelled = True

        async def drain() -> bool:
            await asyncio.gather(pending, return_exceptions=True)
            return False

        await drain_cleanup(drain())
        raise
