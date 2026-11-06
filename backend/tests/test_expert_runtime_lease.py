import asyncio
import threading
from typing import Any, cast

import pytest

from app.exceptions import ConflictError, ServiceOperationError
from app.features.assistant.engines import runtime_lease as engine


class Store:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.set: Any = self._default_set
        self.delete: Any = self._default_delete
        self.eval: Any = self._default_eval

    def _default_set(self, key: str, token: Any, *args: Any, **kwargs: Any) -> bool | None:
        if key in self.values:
            return None
        self.values[key] = token
        return True

    def _default_delete(self, key: str, *args: Any) -> bool:
        return self.values.pop(key, None) is not None

    def _default_eval(self, script: Any, *args: Any) -> int | bool:
        if not args:
            return 0
        keys = args[0]
        assert keys == 1
        key = cast(str, args[1])
        token = args[2]
        if self.values.get(key) != token:
            return 0
        if len(args) > 3:
            return 1
        return self.delete(key)


@pytest.mark.parametrize('lost_reply', [False, True])
async def test_cancelled_acquisition_drains_and_releases_late_redis_lock(monkeypatch: pytest.MonkeyPatch, lost_reply: bool) -> None:
    store = Store()
    entered, release, finished = (threading.Event() for _ in range(3))
    real_set = store.set
    def held_set(key: str, token: Any, *args: Any, **kwargs: Any) -> bool | None:
        result = real_set(key, token, *args, **kwargs)
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
    entered_body: list[bool] = []
    async def caller() -> None:
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
    assert not engine._LOCAL_RUNTIME_LOCKS[81001].locked()  # pyright: ignore[reportPrivateUsage]


async def test_expired_lease_owner_does_not_delete_successor(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    key = 'assistant:expert:runtime:81002'
    async with engine.runtime_lease(81002):
        store.values[key] = 'successor-after-expiry'
    assert store.values[key] == 'successor-after-expiry'
    assert not engine._LOCAL_RUNTIME_LOCKS[81002].locked()  # pyright: ignore[reportPrivateUsage]


async def test_redis_failure_prevents_expert_execution_and_releases_ambiguous_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    real_set = store.set
    def lost_reply(key: str, token: Any, *args: Any, **kwargs: Any) -> bool | None:
        real_set(key, token, *args, **kwargs)
        raise ConnectionError('private-redis-url')
    store.set = lost_reply
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    with pytest.raises(ServiceOperationError) as error:
        async with engine.runtime_lease(81003):
            pytest.fail('Redis failure cannot silently bypass the cross-worker lock')
    assert 'private-redis-url' not in str(error.value)
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81003].locked()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize('body_fault', [False, True])
@pytest.mark.parametrize("release_fault", [False, True])
async def test_release_drains_under_repeat_cancellation_and_preserves_body_error(monkeypatch: pytest.MonkeyPatch, body_fault: bool, release_fault: bool) -> None:
    store = Store()
    entered, release = threading.Event(), threading.Event()
    real_delete = store.delete
    def held_delete(key: str, *args: Any) -> bool:
        entered.set()
        assert release.wait(3)
        result = real_delete(key, *args)
        if release_fault:
            raise RuntimeError("private-release-url")
        return result
    store.delete = held_delete
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    logged: list[str] = []
    def record_error(message: object, *args: object) -> None:
        logged.append(str(message).format(*args))
    monkeypatch.setattr(engine.logger, "error", record_error)
    original = ValueError('fixed-body-error')
    async def caller() -> None:
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
        assert not pending.done() and engine._LOCAL_RUNTIME_LOCKS[81004].locked()  # pyright: ignore[reportPrivateUsage]
    finally:
        release.set()
        with pytest.raises(ValueError if body_fault else asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 1)
    if body_fault:
        assert error.value is original
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81004].locked()  # pyright: ignore[reportPrivateUsage]
    assert "private-release-url" not in str(logged)
    if release_fault:
        assert any("RuntimeError" in message for message in logged)


async def test_busy_redis_lease_is_not_deleted_by_failed_acquirer(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    store.values['assistant:expert:runtime:81005'] = 'other-worker'
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    with pytest.raises(ConflictError):
        async with engine.runtime_lease(81005):
            pytest.fail('busy lease admitted')
    assert store.values['assistant:expert:runtime:81005'] == 'other-worker'
    assert not engine._LOCAL_RUNTIME_LOCKS[81005].locked()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize('loss', ['replacement', 'outage'])
@pytest.mark.parametrize("abort_fault", [False, True])
async def test_renewal_loss_stops_body_and_aborts_before_releasing(monkeypatch: pytest.MonkeyPatch, loss: str, abort_fault: bool) -> None:
    store = Store()
    aborted: list[bool] = []
    body_entered = asyncio.Event()
    key = 'assistant:expert:runtime:81006'
    logged: list[str] = []
    def record_error(message: object, *args: object) -> None:
        logged.append(str(message).format(*args))
    monkeypatch.setattr(engine.logger, "error", record_error)
    monkeypatch.setenv('ASSISTANT_RUNTIME_LOCK_TTL', '1')
    real_eval = store.eval
    def evaluate(script: Any, *args: Any) -> int | bool:
        if len(args) > 3:
            if loss == 'outage':
                raise ConnectionError('private-redis-url')
            store.values[key] = 'successor-worker'
        return real_eval(script, *args)
    store.eval = evaluate
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    async def abort() -> None:
        aborted.append(True)
        if abort_fault:
            raise RuntimeError("private-abort-url")
    async def caller() -> None:
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
    assert not engine._LOCAL_RUNTIME_LOCKS[81006].locked()  # pyright: ignore[reportPrivateUsage]
    assert "private-" not in str(logged)
    if abort_fault:
        assert any("RuntimeError" in message for message in logged)


async def test_renewal_deadline_aborts_before_slow_redis_worker_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    entered, release = threading.Event(), threading.Event()
    aborted = asyncio.Event()
    stopped: list[bool] = []
    real_eval = store.eval
    def held_renew(script: Any, *args: Any) -> int | bool:
        result = real_eval(script, *args)
        if len(args) > 3:
            entered.set()
            assert release.wait(3)
        return result
    store.eval = held_renew
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    monkeypatch.setenv('ASSISTANT_RUNTIME_LOCK_TTL', '1')
    async def abort() -> None:
        aborted.set()
    async def caller() -> None:
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
        assert not pending.done() and engine._LOCAL_RUNTIME_LOCKS[81007].locked()  # pyright: ignore[reportPrivateUsage]
    finally:
        release.set()
        with pytest.raises(ServiceOperationError):
            await asyncio.wait_for(pending, 1)
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81007].locked()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize('failure', [False, True])
async def test_checkpoint_precedes_set_and_preserves_its_error(monkeypatch: pytest.MonkeyPatch, failure: bool) -> None:
    from app.exceptions import PermissionDeniedError
    store = Store()
    calls: list[tuple[str, Any]] = []
    original = PermissionDeniedError('checkpoint-denied')
    real_set = store.set
    def observed_set(key: str, token: Any, *args: Any, **kwargs: Any) -> bool | None:
        assert calls == [('checkpoint', token)]
        calls.append(('set', token))
        return real_set(key, token, *args, **kwargs)
    store.set = observed_set
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    async def checkpoint(token: str) -> None:
        calls.append(('checkpoint', token))
        if failure:
            raise original
    async def caller() -> None:
        async with engine.runtime_lease(81008, before_acquire=checkpoint):
            calls.append(('body', None))
    if failure:
        with pytest.raises(PermissionDeniedError) as error:
            await caller()
        assert error.value is original and len(calls) == 1
    else:
        await caller()
        assert [call[0] for call in calls] == ['checkpoint', 'set', 'body']
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81008].locked()  # pyright: ignore[reportPrivateUsage]


async def test_cancelled_checkpoint_drains_sql_before_unlock_without_redis_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.infra.owned_work import run_owned
    store = Store()
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    monkeypatch.setattr(engine.extensions, 'redis_client', store)
    def persist(token: str) -> None:
        entered.set()
        assert release.wait(3)
        calls.append(token)
    async def checkpoint(token: str) -> None:
        await run_owned(persist, token)
    async def caller() -> None:
        async with engine.runtime_lease(81009, before_acquire=checkpoint):
            pytest.fail('cancelled checkpoint must not execute provider work')
    pending = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel('caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and not store.values and engine._LOCAL_RUNTIME_LOCKS[81009].locked()  # pyright: ignore[reportPrivateUsage]
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 1)
    assert str(error.value) == 'caller-stop' and len(calls) == 1
    assert not store.values and not engine._LOCAL_RUNTIME_LOCKS[81009].locked()  # pyright: ignore[reportPrivateUsage]
