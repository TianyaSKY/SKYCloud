import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.infra.cache import cacheable, cache_evict, cache_put


@pytest.mark.parametrize("operation", ["read", "write", "evict", "put"])
async def test_slow_redis_keeps_event_loop_available(operation):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    redis = MagicMock()
    redis.get.return_value = None

    def slow_operation(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=2)
        return None

    if operation == "read":
        redis.get.side_effect = slow_operation
    elif operation == "evict":
        redis.delete.side_effect = slow_operation
    else:
        redis.setex.side_effect = slow_operation
    decorator = cache_evict if operation == "evict" else cache_put if operation == "put" else cacheable

    @decorator(prefix="test", key=lambda: "key")
    async def work():
        return {"value": 1}

    with patch("app.infra.cache.redis_client", redis):
        task = asyncio.create_task(work())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            assert not task.done(), "Redis blocked the event loop"
        finally:
            release.set()
            assert await task == {"value": 1}


@pytest.mark.parametrize('operation', ['read', 'write', 'evict-before', 'evict-after', 'put'])
async def test_cancelled_cache_call_drains_started_redis_before_return(operation):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    redis = MagicMock()
    redis.get.return_value = None
    effects = []
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        effects.append(operation)
        finished.set()
    if operation == 'read':
        redis.get.side_effect = held
    elif operation.startswith('evict'):
        redis.delete.side_effect = held
    else:
        redis.setex.side_effect = held
    decorator = cache_evict(prefix='owned', before_invocation=operation == 'evict-before') if operation.startswith('evict') else cache_put(prefix='owned') if operation == 'put' else cacheable(prefix='owned')
    @decorator
    async def work():
        return {'value': 1}
    with patch('app.infra.cache.redis_client', redis):
        task = asyncio.create_task(work())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel('caller-stop')
            await asyncio.sleep(.01)
            task.cancel('repeat-stop')
            await asyncio.sleep(.01)
            assert not task.done() and not effects
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(task, 1)
            assert await asyncio.to_thread(finished.wait, 1)
        assert str(error.value) == 'caller-stop' and effects == [operation]


@pytest.mark.parametrize('operation', ['evict-before', 'evict-after', 'put'])
async def test_queued_cache_effect_follows_business_acceptance_policy(monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    entered, release = threading.Event(), threading.Event()
    queued = asyncio.Event()
    business, effects = [], []
    actual_to_thread = asyncio.to_thread
    def hog():
        entered.set()
        assert release.wait(3)
    blocked = asyncio.create_task(actual_to_thread(hog))
    while not entered.is_set():
        await asyncio.sleep(.001)
    async def observed(function, *args, **kwargs):
        queued.set()
        return await actual_to_thread(function, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', observed)
    redis = MagicMock()
    redis.delete.side_effect = lambda *args: effects.append('evict')
    redis.setex.side_effect = lambda *args: effects.append('put')
    decorator = cache_put(prefix='queued') if operation == 'put' else cache_evict(prefix='queued', before_invocation=operation == 'evict-before')
    @decorator
    async def work():
        business.append('accepted')
        return {'value': 1}
    with patch('app.infra.cache.redis_client', redis):
        task = asyncio.create_task(work())
        try:
            await asyncio.wait_for(queued.wait(), 1)
            task.cancel('caller-stop')
            await asyncio.sleep(.01)
            assert not task.done() and not effects
        finally:
            release.set()
            await blocked
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            # Also drain the actual executor in the pre-fix baseline, where
            # the cancelled wrapper has already abandoned its queued work.
            await actual_to_thread(lambda: None)
        assert business == ([] if operation == 'evict-before' else ['accepted'])
        assert effects == ([] if operation == 'evict-before' else ['put' if operation == 'put' else 'evict'])
