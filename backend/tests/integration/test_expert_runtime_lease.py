"""Native Redis proof of expert lease cancellation and token ownership."""
import asyncio
import os
import threading

import pytest

from app.exceptions import ConflictError, ServiceOperationError
from app.features.assistant.engines import runtime_lease as engine
from tests.integration.test_runtime_security import infrastructure  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_EXPERT_GATEWAY_TESTS') != '1', reason='Opt-in native expert Redis lease')


@pytest.mark.parametrize('phase', ['acquire', 'lost-reply', 'release'])
async def test_native_lease_cancellation_drains_redis_and_local_ownership(infrastructure, monkeypatch, phase):
    client = infrastructure['redis']
    entered, release, finished = (threading.Event() for _ in range(3))
    runtime_id = 92001
    key = f'assistant:expert:runtime:{runtime_id}'
    body = []

    class Proxy:
        def set(self, *args, **kwargs):
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

        def eval(self, *args):
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
async def test_native_lease_admission_and_atomic_token_release(infrastructure, monkeypatch, outcome):
    client = infrastructure['redis']
    key = 'assistant:expert:runtime:92002'
    if outcome == 'busy':
        client.set(key, 'other-worker', ex=60)
    class Proxy:
        def set(self, *args, **kwargs):
            result = client.set(*args, **kwargs)
            if outcome == 'lost-reply':
                raise ConnectionError('synthetic-lost-redis-response')
            return result

        def eval(self, *args):
            return client.eval(*args)
    monkeypatch.setattr(engine.extensions, 'redis_client', Proxy())
    if outcome == 'successor':
        async with engine.runtime_lease(92002):
            # Independent Redis ownership changed before the old task exits.
            client.set(key, 'successor-worker', ex=60)
        assert client.get(key) == 'successor-worker'
    else:
        with pytest.raises(ConflictError if outcome == 'busy' else ServiceOperationError):
            async with engine.runtime_lease(92002):
                pytest.fail('failed Redis admission must not execute expert work')
        assert client.get(key) == ('other-worker' if outcome == 'busy' else None)
    assert not engine._LOCAL_RUNTIME_LOCKS[92002].locked()
