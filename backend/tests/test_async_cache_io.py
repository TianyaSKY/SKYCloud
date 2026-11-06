import asyncio
import threading
from functools import partial
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.infra.cache import CacheableParams, cacheable


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_slow_redis_keeps_event_loop_available(operation: str) -> None:
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    redis = MagicMock()
    redis.get.return_value = None

    def slow_operation(*args: object, **kwargs: object) -> None:
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=2)
        return None

    if operation == "read":
        redis.get.side_effect = slow_operation
    else:
        redis.setex.side_effect = slow_operation
    decorator = cacheable(CacheableParams(prefix="test"), key=lambda: "key")

    @decorator
    async def work() -> dict[str, int]:
        return {"value": 1}

    with patch("app.infra.cache.redis_client", redis):
        task = asyncio.create_task(work())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            assert not task.done(), "Redis blocked the event loop"
        finally:
            release.set()
            assert await task == {"value": 1}


@pytest.mark.parametrize('operation', ['read', 'write'])
async def test_cancelled_cache_call_drains_started_redis_before_return(operation: str) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    redis = MagicMock()
    redis.get.return_value = None
    effects: list[str] = []

    def held(*args: object, **kwargs: object) -> None:
        entered.set()
        assert release.wait(3)
        effects.append(operation)
        finished.set()

    if operation == 'read':
        redis.get.side_effect = held
    else:
        redis.setex.side_effect = held
    decorator = cacheable(CacheableParams(prefix='owned'))

    @decorator
    async def work() -> dict[str, int]:
        return {'value': 1}

    with patch('app.infra.cache.redis_client', redis):
        task = asyncio.create_task(work())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel('caller-stop')
            await asyncio.sleep(0.01)
            task.cancel('repeat-stop')
            await asyncio.sleep(0.01)
            assert not task.done() and not effects
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(task, 1)
            assert await asyncio.to_thread(finished.wait, 1)
        assert str(error.value) == 'caller-stop' and effects == [operation]


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_cacheable_preserves_partial_callables(monkeypatch: pytest.MonkeyPatch, asynchronous: bool) -> None:
    redis = MagicMock()
    redis.get.return_value = None
    monkeypatch.setattr("app.infra.cache.redis_client", redis)
    calls: list[tuple[str, int]] = []
    function: Any

    if asynchronous:

        async def async_source(bound: str, value: int) -> dict[str, int]:
            calls.append((bound, value))
            return {"value": value}

        function = partial(async_source, "bound")
    else:

        def sync_source(bound: str, value: int) -> dict[str, int]:
            calls.append((bound, value))
            return {"value": value}

        function = partial(sync_source, "bound")

    def _key(value: int) -> str:
        return str(value)

    decorator_func: Any = cacheable(CacheableParams(prefix="partial"), key=_key)
    decorated: Any = decorator_func(function)
    assert getattr(decorated, "cache_prefix") == "partial"

    result = await decorated(7) if asynchronous else decorated(7)
    assert result == {"value": 7}
    assert calls == [("bound", 7)]
    redis.setex.assert_called_once_with("partial:7", 3600, '{"value": 7}')
