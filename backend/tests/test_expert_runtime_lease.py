import asyncio
import threading

import pytest

from app.exceptions import ConflictError, ServiceOperationError
from app.features.assistant.engines import runtime_lease as engine


class Store:
    def __init__(self):
        self.values = {}

    def set(self, key, token, **kwargs):
        if key in self.values:
            return None
        self.values[key] = token
        return True

    def delete(self, key):
        return self.values.pop(key, None) is not None

    def eval(self, script, keys, key, token):
        assert keys == 1
        if self.values.get(key) != token:
            return 0
        return self.delete(key)


@pytest.mark.parametrize('lost_reply', [False, True])
async def test_cancelled_acquisition_drains_and_releases_late_redis_lock(monkeypatch, lost_reply):
    store = Store()
    entered, release, finished = (threading.Event() for _ in range(3))
    real_set = store.set
    def held_set(*args, **kwargs):
        result = real_set(*args, **kwargs)
        entered.set()
        try:
            assert release.wait(3)
            if lost_reply:
                raise ConnectionError('private-redis-url')
            return result
        finally:
            finished.set()
    store.set = held_set
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    entered_body = []
    async def caller():
        async with engine.runtime_lease(81001):
            entered_body.append(True)
    pending = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel('original-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done(), 'Redis acquisition must finish before lease teardown'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
        assert await asyncio.to_thread(finished.wait, 1)
    assert not store.values and not entered_body
    assert not engine._LOCAL_RUNTIME_LOCKS[81001].locked()


async def test_expired_lease_owner_does_not_delete_successor(monkeypatch):
    store = Store()
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    key = 'assistant:expert:runtime:81002'
    async with engine.runtime_lease(81002):
        store.values[key] = 'successor-after-expiry'
    assert store.values[key] == 'successor-after-expiry'
    assert not engine._LOCAL_RUNTIME_LOCKS[81002].locked()


async def test_redis_failure_prevents_expert_execution_and_releases_ambiguous_lock(monkeypatch):
    store = Store()
    real_set = store.set
    def lost_reply(*args, **kwargs):
        real_set(*args, **kwargs)
        raise ConnectionError('private-redis-url')
    store.set = lost_reply
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    with pytest.raises(ServiceOperationError) as error:
        async with engine.runtime_lease(81003):
            pytest.fail('Redis failure cannot silently bypass the cross-worker lock')
    assert 'private-redis-url' not in str(error.value)
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81003].locked()


@pytest.mark.parametrize('body_fault', [False, True])
async def test_release_drains_under_repeat_cancellation_and_preserves_body_error(monkeypatch, body_fault):
    store = Store()
    entered, release = threading.Event(), threading.Event()
    real_delete = store.delete
    def held_delete(*args):
        entered.set()
        assert release.wait(3)
        return real_delete(*args)
    store.delete = held_delete
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    original = ValueError('fixed-body-error')
    async def caller():
        async with engine.runtime_lease(81004):
            if body_fault:
                raise original
    pending = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel('release-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and engine._LOCAL_RUNTIME_LOCKS[81004].locked()
    finally:
        release.set()
        with pytest.raises(ValueError if body_fault else asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 1)
    if body_fault:
        assert error.value is original
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81004].locked()


async def test_busy_redis_lease_is_not_deleted_by_failed_acquirer(monkeypatch):
    store = Store()
    store.values['assistant:expert:runtime:81005'] = 'other-worker'
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    with pytest.raises(ConflictError):
        async with engine.runtime_lease(81005):
            pytest.fail('busy lease admitted')
    assert store.values['assistant:expert:runtime:81005'] == 'other-worker'
    assert not engine._LOCAL_RUNTIME_LOCKS[81005].locked()
