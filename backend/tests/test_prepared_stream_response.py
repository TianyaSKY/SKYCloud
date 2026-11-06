# pyright: reportPrivateUsage=false
"""已准备的 HTTP 运行在交付失败后不得保持活跃状态测试."""
import asyncio
from collections.abc import AsyncIterator
import threading
from typing import Any, cast

import pytest
from starlette.requests import ClientDisconnect

from app.features.assistant import repository, service, stream_response
from app.models.assistant import AssistantMessage


@pytest.mark.parametrize('state', ['unclaimed', 'leased', 'completed', 'foreign-user', 'foreign-workspace', 'foreign-conversation'])
def test_only_exact_unclaimed_run_cancelled(session: Any, test_user: Any, test_workspace: Any, state: str) -> None:
    conversation = service.create_conversation(session, test_workspace, test_user.id, 'fast', 'test')
    _, run, _ = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            query='question',
        ),
    )
    scope = [test_workspace.id, test_user.id, conversation.id, run.id]
    if state == 'leased':
        repository.touch_run_lease(session, run.id, 1)
        session.commit()
    elif state == 'completed':
        repository.update_run_status(session, run, "completed")
        msg = session.get(AssistantMessage, run.assistant_message_id)
        assert msg is not None
        msg.status = 'completed'
        session.commit()
    elif state.startswith('foreign-'):
        scope[['foreign-workspace', 'foreign-user', 'foreign-conversation'].index(state)] += 1000
    assert repository.cancel_unclaimed_run(session, *scope) is (state == 'unclaimed')
    session.commit()
    session.refresh(run)
    message = session.get(AssistantMessage, run.assistant_message_id)
    assert message is not None
    expected = 'cancelled' if state == 'unclaimed' else 'completed' if state == 'completed' else 'streaming'
    assert message.status == expected
    assert run.status == (expected if expected != 'streaming' else 'running')
    if state == 'unclaimed':
        assert run.cancel_requested and run.completed_at is not None
        assert repository.cancel_unclaimed_run(session, *scope) is False


@pytest.mark.parametrize('failure', ['headers', 'body', 'cancel'])
async def test_delivery_failure_closes_iterator_and_compensates_on_worker(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    calls: list[str] = []
    loop_thread = threading.get_ident()
    entered = closed = False
    async def body() -> AsyncIterator[bytes]:
        nonlocal entered, closed
        entered = True
        try:
            yield b'first'
        finally:
            closed = True
    class Session:
        def __init__(self) -> None:
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
        def check(self) -> None:
            assert threading.get_ident() == self.owner
        def commit(self) -> None:
            self.check()
            calls.append('commit')
        def close(self) -> None:
            self.check()
            calls.append('close')
    def compensate(session: Any, *scope: Any) -> None:
        session.check()
        assert scope == (1, 2, 3, 4)
        calls.append('cancel')
    monkeypatch.setattr(repository, 'cancel_unclaimed_run', compensate)
    response = stream_response.PreparedStreamResponse(body(), session_factory=cast(Any, Session), run_scope=(1, 2, 3, 4))
    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}
    async def send(message: Any) -> None:
        if failure == 'headers' or message['type'] == 'http.response.body':
            if failure == 'cancel':
                raise asyncio.CancelledError()
            raise OSError('connection lost')
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else ClientDisconnect):
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    assert entered is (failure != 'headers')
    assert closed is (failure != 'headers')
    assert calls == ['cancel', 'commit', 'close']


async def test_cleanup_fault_preserves_delivery_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[str] = []
    def log_error(msg: str, *args: Any) -> None:
        logged.append(msg.format(*args))
    monkeypatch.setattr(stream_response.logger, 'error', log_error)
    def broken() -> Any:
        raise RuntimeError('private-db-url')
    async def body() -> AsyncIterator[bytes]:
        yield b'x'
    response = stream_response.PreparedStreamResponse(body(), session_factory=cast(Any, broken), run_scope=(1, 2, 3, 4))
    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}
    async def send(message: Any) -> None:
        raise OSError('connection lost')
    with pytest.raises(ClientDisconnect):
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    assert logged and 'private-db-url' not in str(logged)


async def test_anyio_disconnect_scope_still_drains_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    closed = False
    cleanup: list[Any] = []
    loop_thread = threading.get_ident()
    async def body() -> AsyncIterator[bytes]:
        nonlocal closed
        entered.set()
        try:
            await asyncio.Event().wait()
            yield b'unreachable'
        finally:
            closed = True
    def compensate(factory: Any, scope: Any) -> None:
        assert closed and threading.get_ident() != loop_thread
        cleanup.append(scope)
    monkeypatch.setattr(stream_response, 'cleanup_unclaimed_run', compensate)
    response = stream_response.PreparedStreamResponse(body(), session_factory=cast(Any, None), run_scope=(1, 2, 3, 4))
    async def receive() -> dict[str, Any]:
        await entered.wait()
        return {'type': 'http.disconnect'}
    async def send(message: Any) -> None:
        pass
    await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send), 2)
    assert closed and cleanup == [(1, 2, 3, 4)]


@pytest.mark.parametrize('created', [False, True])
async def test_prepare_cancellation_drains_before_compensation(monkeypatch: pytest.MonkeyPatch, created: bool) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    owner: list[int] = []
    def prepare(capture: Any) -> tuple[int, int, int, int, list[Any]]:
        owner.append(threading.get_ident())
        if created:
            capture((1, 2, 3, 4))
        entered.set()
        assert release.wait(5)
        calls.extend(['commit', 'close'])
        return (1, 2, 3, 4, [])
    def cleanup(factory: Any, scope: Any) -> None:
        assert threading.get_ident() == owner[0]
        assert calls == ['commit', 'close']
        calls.append('compensate')
    monkeypatch.setattr(stream_response, 'cleanup_unclaimed_run', cleanup)
    pending = asyncio.create_task(stream_response.prepare_response_run(prepare, cast(Any, None)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not calls
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert calls == ['commit', 'close'] + (['compensate'] if created else [])


async def test_prepare_anyio_cancellation_drains_then_compensates(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    scopes: list[anyio.CancelScope] = []
    def prepare(capture: Any) -> None:
        capture((1, 2, 3, 4))
        entered.set()
        assert release.wait(5)
        calls.append('commit')
    def cleanup(factory: Any, scope: Any) -> None:
        assert calls == ['commit']
        calls.append('compensate')
    monkeypatch.setattr(stream_response, 'cleanup_unclaimed_run', cleanup)
    async def request() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await stream_response.prepare_response_run(prepare, cast(Any, None))
        calls.append('cancelled')
    pending = asyncio.create_task(request())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        scopes[0].cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not calls
    finally:
        release.set()
    await asyncio.wait_for(pending, 2)
    assert calls == ['commit', 'compensate', 'cancelled']


@pytest.mark.parametrize('created', [False, True])
async def test_prepare_original_error_preserved(monkeypatch: pytest.MonkeyPatch, created: bool) -> None:
    original = RuntimeError('original preparation failure')
    calls: list[str] = []
    def prepare(capture: Any) -> None:
        if created:
            capture((1, 2, 3, 4))
        raise original
    def fake_cleanup(*_args: Any, **_kwargs: Any) -> None:
        calls.append('cleanup')
    monkeypatch.setattr(stream_response, 'cleanup_unclaimed_run', fake_cleanup)
    with pytest.raises(RuntimeError) as error:
        await stream_response.prepare_response_run(prepare, cast(Any, None))
    assert error.value is original
    assert calls == (['cleanup'] if created else [])


def test_prepare_captures_id_before_commit_failure(session: Any, test_user: Any, test_workspace: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    conversation = service.create_conversation(session, test_workspace, test_user.id, 'fast', 'test')
    captured: list[tuple[int, int, int, int]] = []
    def fail() -> None:
        raise RuntimeError('commit failure')
    monkeypatch.setattr(session, 'commit', fail)
    with pytest.raises(RuntimeError, match='commit failure'):
        service.prepare_run(
            session,
            service.PrepareRunParams(
                workspace=test_workspace,
                user_id=test_user.id,
                conversation_id=conversation.id,
                query='question',
                on_run_created=captured.append,
            ),
        )
    assert len(captured) == 1
    workspace_id, user_id, conversation_id, run_id = captured[0]
    assert (workspace_id, user_id, conversation_id) == (test_workspace.id, test_user.id, conversation.id)
    assert run_id > 0


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('invalidation_fails', [False, True])
async def test_preparation_close_fault_preserves_result_and_original_error(monkeypatch: pytest.MonkeyPatch, failed: bool, invalidation_fails: bool) -> None:
    calls: list[str] = []
    logged: list[str] = []
    original = RuntimeError('original operation error')
    loop_thread = threading.get_ident()
    class Owned:
        def __init__(self) -> None:
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
        def close(self) -> None:
            assert threading.get_ident() == self.owner
            calls.append('close')
            raise RuntimeError('private-close-url')
        def invalidate(self) -> None:
            assert threading.get_ident() == self.owner
            calls.append('invalidate')
            if invalidation_fails:
                raise RuntimeError('private-invalidate-url')
    def prepare(capture: Any) -> str:
        with stream_response.preparation_session(cast(Any, Owned)):
            if failed:
                raise original
            return 'prepared'
    def log_error(msg: str, *args: Any) -> None:
        logged.append(msg.format(*args))
    monkeypatch.setattr(service.logger, 'error', log_error)
    if failed:
        with pytest.raises(RuntimeError) as error:
            await stream_response.prepare_response_run(prepare, cast(Any, Owned))
        assert error.value is original
    else:
        assert await stream_response.prepare_response_run(prepare, cast(Any, Owned)) == 'prepared'
    assert calls == ['close', 'invalidate']
    assert logged and 'private-' not in str(logged)


@pytest.mark.parametrize('delivery', ['headers', 'cancel', 'normal'])
@pytest.mark.parametrize('cleanup', ['self-cancel', 'repeat-cancel'])
async def test_response_teardown_cancellation_preserves_delivery_and_compensates(monkeypatch: pytest.MonkeyPatch, delivery: str, cleanup: str) -> None:
    closing, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    logs: list[str] = []
    loop_thread = threading.get_ident()
    class Body:
        def __aiter__(self) -> 'Body':
            return self
        async def __anext__(self) -> bytes:
            raise StopAsyncIteration
        async def aclose(self) -> None:
            closing.set()
            if cleanup == 'repeat-cancel':
                await release.wait()
            calls.append('close')
            if cleanup == 'self-cancel':
                raise asyncio.CancelledError('private-provider-close')
    def compensate(factory: Any, scope: Any) -> None:
        assert threading.get_ident() != loop_thread and calls == ['close']
        assert scope == (1, 2, 3, 4)
        calls.append('compensate')
    monkeypatch.setattr(stream_response, 'cleanup_unclaimed_run', compensate)
    from runtime_common import http_stream
    def log_error(message: str, *args: Any) -> None:
        logs.append(message % args)
    monkeypatch.setattr(http_stream.logger, 'error', log_error)
    response = stream_response.PreparedStreamResponse(Body(), session_factory=cast(Any, None), run_scope=(1, 2, 3, 4))
    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}
    async def send(message: Any) -> None:
        if delivery == 'headers':
            raise OSError('header delivery failed')
        if delivery == 'cancel':
            raise asyncio.CancelledError('delivery-cancel')
    pending = asyncio.create_task(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        if cleanup == 'repeat-cancel':
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0.01)
            assert not pending.done(), 'iterator teardown was interrupted'
    finally:
        release.set()
        if delivery == 'headers':
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(pending, 1)
        elif delivery == 'cancel' or cleanup == 'repeat-cancel':
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending, 1)
            assert 'private-provider-close' not in str(error.value)
        else:
            await asyncio.wait_for(pending, 1)
    assert calls == ['close', 'compensate']
    assert ('CancelledError' in str(logs)) is (cleanup == 'self-cancel')
    assert 'private-provider-close' not in str(logs)
