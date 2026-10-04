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
