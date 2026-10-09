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

    def eval(self, script, keys, key, token, *args):
        assert keys == 1
        if self.values.get(key) != token:
            return 0
        if args:
            return 1
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


@pytest.mark.parametrize('loss', ['replacement', 'outage'])
async def test_renewal_loss_stops_body_and_aborts_before_releasing(monkeypatch, loss):
    store = Store()
    aborted = []
    body_entered = asyncio.Event()
    key = 'assistant:expert:runtime:81006'
    monkeypatch.setenv('ASSISTANT_RUNTIME_LOCK_TTL', '1')
    real_eval = store.eval
    def evaluate(script, *args):
        if len(args) > 3:
            if loss == 'outage':
                raise ConnectionError('private-redis-url')
            store.values[key] = 'successor-worker'
        return real_eval(script, *args)
    store.eval = evaluate
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    async def abort():
        aborted.append(True)
    async def caller():
        async with engine.runtime_lease(81006, on_lost=abort):
            body_entered.set()
            await asyncio.Event().wait()
    pending = asyncio.create_task(caller())
    try:
        with pytest.raises(ServiceOperationError) as error:
            await asyncio.wait_for(pending, 2)
        assert 'private-redis-url' not in str(error.value)
        assert body_entered.is_set() and aborted == [True]
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert store.values == ({key: 'successor-worker'} if loss == 'replacement' else {})
    assert not engine._LOCAL_RUNTIME_LOCKS[81006].locked()


async def test_renewal_deadline_aborts_before_slow_redis_worker_finishes(monkeypatch):
    store = Store()
    entered, release = threading.Event(), threading.Event()
    aborted = asyncio.Event()
    stopped = []
    real_eval = store.eval
    def held_renew(script, *args):
        result = real_eval(script, *args)
        if len(args) > 3:
            entered.set()
            assert release.wait(3)
        return result
    store.eval = held_renew
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    monkeypatch.setenv('ASSISTANT_RUNTIME_LOCK_TTL', '1')
    async def abort():
        aborted.set()
    async def caller():
        async with engine.runtime_lease(81007, on_lost=abort):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)
    pending = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(aborted.wait(), 2)
        assert stopped == [True] and not pending.done()
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and engine._LOCAL_RUNTIME_LOCKS[81007].locked()
    finally:
        release.set()
        with pytest.raises(ServiceOperationError):
            await asyncio.wait_for(pending, 1)
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81007].locked()


@pytest.mark.parametrize('failure', [False, True])
async def test_checkpoint_precedes_set_and_preserves_its_error(monkeypatch, failure):
    from app.exceptions import PermissionDeniedError
    store = Store()
    calls = []
    original = PermissionDeniedError('checkpoint-denied')
    real_set = store.set
    def observed_set(key, token, **kwargs):
        assert calls == [('checkpoint', token)]
        calls.append(('set', token))
        return real_set(key, token, **kwargs)
    store.set = observed_set
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    async def checkpoint(token):
        calls.append(('checkpoint', token))
        if failure:
            raise original
    async def caller():
        async with engine.runtime_lease(81008, before_acquire=checkpoint):
            calls.append(('body', None))
    if failure:
        with pytest.raises(PermissionDeniedError) as error:
            await caller()
        assert error.value is original and len(calls) == 1
    else:
        await caller()
        assert [call[0] for call in calls] == ['checkpoint', 'set', 'body']
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81008].locked()


async def test_cancelled_checkpoint_drains_sql_before_unlock_without_redis_set(monkeypatch):
    from app.infra.owned_work import run_owned
    store = Store()
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    def persist(token):
        entered.set()
        assert release.wait(3)
        calls.append(token)
    async def checkpoint(token):
        await run_owned(persist, token)
    async def caller():
        async with engine.runtime_lease(81009, before_acquire=checkpoint):
            pytest.fail('cancelled checkpoint must not execute provider work')
    pending = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel('caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and not store.values and engine._LOCAL_RUNTIME_LOCKS[81009].locked()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 1)
    assert str(error.value) == 'caller-stop' and len(calls) == 1
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81009].locked()
