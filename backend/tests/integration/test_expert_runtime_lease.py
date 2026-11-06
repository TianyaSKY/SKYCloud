from typing import Any
# pyright: reportPrivateUsage=false
"""专家租约取消与令牌所有权的原生 Redis 验证."""
import asyncio
import os
import threading

import pytest

from app.exceptions import ConflictError, ServiceOperationError
from app.features.assistant.engines import runtime_lease as engine
from tests.integration.test_runtime_security import infrastructure

__all__ = ["infrastructure"]  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_EXPERT_GATEWAY_TESTS') != '1', reason='Opt-in native expert Redis lease')


@pytest.mark.parametrize('phase', ['acquire', 'lost-reply', 'release'])
async def test_native_lease_cancellation_drains_redis_and_local_ownership(infrastructure: Any, monkeypatch: Any, phase: Any) -> None:
    client = infrastructure['redis']
    entered, release, finished = (threading.Event() for _ in range(3))
    runtime_id = 92001
    key = f'assistant:expert:runtime:{runtime_id}'
    body: list[bool] = []

    class Proxy:
        def set(self, *args: Any, **kwargs: Any):
            result = client.set(*args, **kwargs)
            if phase != 'release':
                entered.set()
                try:
                    assert release.wait(3)
                    if phase == 'lost-reply':
                        raise ConnectionError('synthetic-lost-redis-response')
                finally:
                    finished.set()
            return result

        def eval(self, *args: Any):
            if phase == 'release':
                entered.set()
                try:
                    assert release.wait(3)
                finally:
                    finished.set()
            return client.eval(*args)

    monkeypatch.setattr(engine.extensions, 'redis_client', Proxy())
    async def caller():
        async with engine.runtime_lease(runtime_id):
            body.append(True)
    pending = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert client.get(key) is not None
        pending.cancel('native-caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and engine._LOCAL_RUNTIME_LOCKS[runtime_id].locked()
        assert client.get(key) is not None
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
        assert await asyncio.to_thread(finished.wait, 1)
    assert client.get(key) is None
    assert body == ([True] if phase == 'release' else [])
    assert not engine._LOCAL_RUNTIME_LOCKS[runtime_id].locked()


@pytest.mark.parametrize('outcome', ['busy', 'lost-reply', 'successor'])
async def test_native_lease_admission_and_atomic_token_release(infrastructure: Any, monkeypatch: Any, outcome: Any) -> None:
    client = infrastructure['redis']
    key = 'assistant:expert:runtime:92002'
    if outcome == 'busy':
        client.set(key, 'other-worker', ex=60)
    class Proxy:
        def set(self, *args: Any, **kwargs: Any):
            result = client.set(*args, **kwargs)
            if outcome == 'lost-reply':
                raise ConnectionError('synthetic-lost-redis-response')
            return result

        def eval(self, *args: Any):
            return client.eval(*args)
    monkeypatch.setattr(engine.extensions, 'redis_client', Proxy())
    if outcome == 'successor':
        async with engine.runtime_lease(92002):
            # 独立的 Redis 所有权在旧任务退出前发生变更.
            client.set(key, 'successor-worker', ex=60)
        assert client.get(key) == 'successor-worker'
    else:
        with pytest.raises(ConflictError if outcome == 'busy' else ServiceOperationError):
            async with engine.runtime_lease(92002):
                pytest.fail('failed Redis admission must not execute expert work')
        assert client.get(key) == ('other-worker' if outcome == 'busy' else None)
    assert not engine._LOCAL_RUNTIME_LOCKS[92002].locked()


async def test_native_lease_stays_owned_beyond_original_ttl(infrastructure: Any, monkeypatch: Any) -> None:
    client = infrastructure['redis']
    key = 'assistant:expert:runtime:92003'
    monkeypatch.setattr(engine.extensions, 'redis_client', client)
    monkeypatch.setenv('ASSISTANT_RUNTIME_LOCK_TTL', '1')
    async with engine.runtime_lease(92003):
        token = client.get(key)
        assert token is not None
        await asyncio.sleep(2.1)
        assert client.get(key) == token, 'active expert lease expired while its task still runs'
        assert client.set(key, 'other-worker', nx=True, ex=60) is None
    assert not client.exists(key)


@pytest.mark.parametrize('loss', ['replacement', 'outage'])
async def test_native_renewal_loss_aborts_owner_and_preserves_successor(infrastructure: Any, monkeypatch: Any, loss: Any) -> None:
    client = infrastructure['redis']
    key = 'assistant:expert:runtime:92004'
    aborted: list[bool] = []
    stopped: list[bool] = []
    class Proxy:
        def set(self, *args: Any, **kwargs: Any):
            return client.set(*args, **kwargs)

        def eval(self, script: Any, *args: Any):
            if len(args) > 3:
                if loss == 'outage':
                    raise ConnectionError('synthetic-renewal-unavailable')
                client.set(key, 'successor-worker', ex=60)
            return client.eval(script, *args)
    monkeypatch.setattr(engine.extensions, 'redis_client', Proxy())
    monkeypatch.setenv('ASSISTANT_RUNTIME_LOCK_TTL', '1')
    async def abort():
        aborted.append(True)
    async def caller():
        async with engine.runtime_lease(92004, on_lost=abort):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)
    with pytest.raises(ServiceOperationError):
        await asyncio.wait_for(caller(), 2)
    assert aborted == stopped == [True]
    assert client.get(key) == ('successor-worker' if loss == 'replacement' else None)
    assert not engine._LOCAL_RUNTIME_LOCKS[92004].locked()
