"""Cancellation must drain SQL work before Session cleanup in its owner thread."""
import asyncio
import threading

import pytest

from app.features.assistant import service
from app.features.assistant.stream_persistence import StreamWorker


async def test_worker_serializes_and_drains_repeated_cancellation():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow():
        calls.append(('start', threading.get_ident()))
        entered.set()
        assert release.wait(5)
        calls.append(('finish', threading.get_ident()))
    async with StreamWorker() as worker:
        operation = asyncio.create_task(worker.call(slow))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
            cleanup = asyncio.create_task(worker.call(lambda: calls.append(('close', threading.get_ident()))))
            await asyncio.sleep(0)
            assert not cleanup.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await cleanup
    assert [action for action, _ in calls] == ['start', 'finish', 'close']
    assert len({thread for _, thread in calls}) == 1
    assert calls[0][1] != threading.get_ident()


async def test_cancel_during_stream_session_creation_closes_on_owner_thread():
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Session:
        def __init__(self):
            self.owner = threading.get_ident()
            calls.append('create')
        def get(self, model, identifier):
            assert threading.get_ident() == self.owner
            entered.set()
            assert release.wait(5)
            return None  # Lookup failure after cancellation must still close.
        def close(self):
            assert threading.get_ident() == self.owner
            calls.append('close')
    events = service.stream_run(Session, 1, 2, 3, 4, [])
    operation = asyncio.create_task(anext(events))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await events.aclose()
    assert calls == ['create', 'close']


async def test_actual_stream_sql_runs_on_worker_while_loop_is_responsive(session, test_user, test_workspace, monkeypatch):
    from sqlalchemy import event
    from app.features.assistant import repository
    from app.features.assistant.event_protocol import AssistantEvent
    conversation = repository.create_conversation(session, test_workspace.id, test_user.id, 'fast', 'Slow SQL')
    session.commit()
    conversation, run, history = service.prepare_run(session, test_workspace, test_user.id, conversation.id, 'query')
    class Engine:
        async def stream(self, **kwargs):
            yield AssistantEvent('token', {'content': 'Answer'})
    monkeypatch.setattr(service, 'FastEngine', Engine)
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    owners = set()
    def record(conn, cursor, statement, parameters, context, executemany):
        assert threading.get_ident() != loop_thread
        owners.add(threading.get_ident())
        if statement.startswith('UPDATE assistant_messages'):
            entered.set()
            assert release.wait(5)
    user_id = test_user.id
    engine = session.get_bind()
    event.listen(engine, 'before_cursor_execute', record)
    async def collect():
        return [item async for item in service.stream_prepared_run(session, test_workspace,
                 user_id, conversation, run, history)]
    operation = asyncio.create_task(collect())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        async def probe():
            assert not release.is_set()
            return True
        assert await asyncio.wait_for(probe(), 1)
        assert not operation.done()
    finally:
        release.set()
        try:
            events = await operation
        finally:
            event.remove(engine, 'before_cursor_execute', record)
    assert [item.type for item in events] == ['run_started', 'token', 'done']
    assert len(owners) == 1


def test_replayed_permission_preserves_response_and_finish_preserves_terminal(session, test_user, test_workspace):
    from app.features.assistant import repository
    from app.features.assistant.stream_persistence import StreamPersistence
    conversation = repository.create_conversation(session, test_workspace.id, test_user.id, 'expert', 'Permission')
    user_message = repository.create_message(session, conversation, 'user', 'query')
    assistant = repository.create_message(session, conversation, 'assistant', '', status='streaming')
    run = repository.create_run(session, conversation, user_message, assistant)
    payload = {'permission_id': 'p1', 'title': 'Execute'}
    repository.set_pending_permission(run, payload)
    session.commit()
    persistence = StreamPersistence(session, test_workspace, test_user.id, conversation, run)
    assert repository.set_permission_response(session, run.id, 'p1', 'once')
    session.commit()
    assert persistence.checkpoint('', {}, {}, payload, True)
    session.refresh(run)
    assert run.permission_response == 'once' and run.status == 'running'
    assert not repository.set_permission_response(session, run.id, 'p1', 'reject')
    repository.update_run_status(run, 'failed')
    assistant.content = 'Existing final answer'
    assistant.status = 'failed'
    session.commit()
    assert persistence.finish('completed', 'Late answer', {}, {}, []) == 'failed'
    session.refresh(run)
    session.refresh(assistant)
    assert run.status == assistant.status == 'failed'
    assert assistant.content == 'Existing final answer'


async def test_http_stream_disconnect_closes_underlying_iterator(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app.features.assistant import router as module
    from app.features.assistant.schemas import AssistantMessageRequest
    from app.features.assistant.event_protocol import AssistantEvent
    conversation = SimpleNamespace(id=3, mode='fast')
    run = SimpleNamespace(id=4)
    closed = []
    monkeypatch.setattr(service, 'get_user_conversation', lambda *args: conversation)
    monkeypatch.setattr(service, 'prepare_run', lambda *args, **kwargs: (conversation, run, []))
    async def events(*args):
        try:
            yield AssistantEvent('run_started', {'status': 'running'}, run_id=4)
            await asyncio.Event().wait()
        finally:
            closed.append(True)
    monkeypatch.setattr(service, 'stream_run', events)
    response = await module.stream_assistant_message(3, AssistantMessageRequest(query='query'),
        SimpleNamespace(headers={}), current_user=SimpleNamespace(id=1),
        workspace=SimpleNamespace(id=2), session_factory=Mock)
    assert 'run_started' in await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert closed == [True]
