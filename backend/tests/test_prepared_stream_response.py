"""Prepared HTTP runs must not remain active after delivery fails."""
import asyncio
import threading

import pytest
from starlette.requests import ClientDisconnect

from app.features.assistant import repository, service, stream_response
from app.models.assistant import AssistantMessage


@pytest.mark.parametrize('state', ['unclaimed', 'leased', 'completed', 'foreign-user', 'foreign-workspace', 'foreign-conversation'])
def test_only_exact_unclaimed_run_cancelled(session, test_user, test_workspace, state):
    conversation = service.create_conversation(session, test_workspace, test_user.id, 'fast', 'test')
    _, run, _ = service.prepare_run(session, test_workspace, test_user.id, conversation.id, 'question')
    scope = [test_workspace.id, test_user.id, conversation.id, run.id]
    if state == 'leased':
        repository.touch_run_lease(session, run.id, 1)
    elif state == 'completed':
        repository.update_run_status(run, 'completed')
        session.get(AssistantMessage, run.assistant_message_id).status = 'completed'
        session.commit()
    elif state.startswith('foreign-'):
        scope[['foreign-workspace', 'foreign-user', 'foreign-conversation'].index(state)] += 1000
    assert repository.cancel_unclaimed_run(session, *scope) is (state == 'unclaimed')
    session.commit()
    session.refresh(run)
    message = session.get(AssistantMessage, run.assistant_message_id)
    expected = 'cancelled' if state == 'unclaimed' else 'completed' if state == 'completed' else 'streaming'
    assert message.status == expected
    assert run.status == (expected if expected != 'streaming' else 'running')
    if state == 'unclaimed':
        assert run.cancel_requested and run.completed_at is not None
        assert repository.cancel_unclaimed_run(session, *scope) is False


@pytest.mark.parametrize('failure', ['headers', 'body', 'cancel'])
async def test_delivery_failure_closes_iterator_and_compensates_on_worker(monkeypatch, failure):
    calls = []
    loop_thread = threading.get_ident()
    entered = closed = False
    async def body():
        nonlocal entered, closed
        entered = True
        try:
            yield b'first'
        finally:
            closed = True
    class Session:
        def __init__(self):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
        def check(self): assert threading.get_ident() == self.owner
        def commit(self): self.check(); calls.append('commit')
        def close(self): self.check(); calls.append('close')
    def compensate(session, *scope):
        session.check()
        assert scope == (1, 2, 3, 4)
        calls.append('cancel')
    monkeypatch.setattr(repository, 'cancel_unclaimed_run', compensate)
    response = stream_response.PreparedStreamResponse(body(), session_factory=Session, run_scope=(1, 2, 3, 4))
    async def receive(): await asyncio.Event().wait()
    async def send(message):
        if failure == 'headers' or message['type'] == 'http.response.body':
            if failure == 'cancel': raise asyncio.CancelledError()
            raise OSError('connection lost')
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else ClientDisconnect):
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    assert entered is (failure != 'headers')
    assert closed is (failure != 'headers')
    assert calls == ['cancel', 'commit', 'close']


async def test_cleanup_fault_preserves_delivery_failure(monkeypatch):
    logged = []
    monkeypatch.setattr(stream_response.logger, 'error', lambda msg, *args: logged.append(msg.format(*args)))
    def broken(): raise RuntimeError('private-db-url')
    async def body(): yield b'x'
    response = stream_response.PreparedStreamResponse(body(), session_factory=broken, run_scope=(1, 2, 3, 4))
    async def receive(): await asyncio.Event().wait()
    async def send(message): raise OSError('connection lost')
    with pytest.raises(ClientDisconnect):
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    assert logged and 'private-db-url' not in str(logged)


async def test_anyio_disconnect_scope_still_drains_cleanup(monkeypatch):
    entered = asyncio.Event()
    closed = False
    cleanup = []
    loop_thread = threading.get_ident()
    async def body():
        nonlocal closed
        entered.set()
        try:
            await asyncio.Event().wait()
            yield b'unreachable'
        finally:
            closed = True
    def compensate(factory, scope):
        assert closed and threading.get_ident() != loop_thread
        cleanup.append(scope)
    monkeypatch.setattr(stream_response, 'cleanup_unclaimed_run', compensate)
    response = stream_response.PreparedStreamResponse(body(), session_factory=None, run_scope=(1, 2, 3, 4))
    async def receive():
        await entered.wait()
        return {'type': 'http.disconnect'}
    async def send(message): pass
    await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send), 2)
    assert closed and cleanup == [(1, 2, 3, 4)]
