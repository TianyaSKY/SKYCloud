"""Runtime provisioning has independent worker-owned DB sessions."""
import asyncio
import threading
from contextlib import contextmanager
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.features.assistant.engines import expert_engine as module


@pytest.mark.parametrize('needs_start', [False, True])
async def test_runtime_setup_uses_independent_sessions_and_keeps_loop_responsive(monkeypatch, needs_start):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    sessions, closed = [], []
    runtime = SimpleNamespace(id=9, status='running', container_id='abc', error_message=None)
    workspace = SimpleNamespace(id=2)
    @contextmanager
    def factory():
        thread = threading.get_ident()
        assert thread != loop_thread
        def check_thread(*args):
            assert threading.get_ident() == thread
        session = SimpleNamespace(get=lambda *args: (check_thread(), workspace)[1], commit=check_thread)
        sessions.append(session)
        try:
            yield session
        finally:
            check_thread()
            closed.append(session)
    stream_session = Mock()
    engine = module.ExpertEngine(session=stream_session, conversation=SimpleNamespace(id=3), run=SimpleNamespace(id=4))
    engine.runtime_session_factory = factory
    monkeypatch.setattr(module, 'assert_can_write', lambda session, *args: sessions.index(session))
    monkeypatch.setattr(module.runtime_service, 'get_or_create_runtime', lambda *args: runtime)
    monkeypatch.setattr(module.runtime_service, 'get_runtime', lambda *args: runtime)
    monkeypatch.setattr(module.docker_service, 'runtime_uses_configured_image', lambda *args: not needs_start)
    calls = []
    def block(operation):
        def call(session, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            assert session in sessions
            calls.append(operation)
            entered.set()
            assert release.wait(5)
        return call
    monkeypatch.setattr(module.docker_service, 'start', block('start'))
    monkeypatch.setattr(module.docker_service, 'setup_mcp', block('mcp'))
    monkeypatch.setattr(module.docker_service, 'get_runtime_base_url', lambda *args: 'http://controller/runtime')
    events = engine._ensure_runtime(2, 1)
    first_connection, first_event = await anext(events)
    assert first_connection is None
    assert first_event.payload['runtime_status'] == ('starting' if needs_start else 'running')
    pending = asyncio.create_task(anext(events))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # This coroutine can run while Controller work blocks the worker.
        async def probe():
            assert not release.is_set()
            return True
        assert await asyncio.wait_for(probe(), 1)
        assert not pending.done()
    finally:
        release.set()
        connection, status = await pending
        await events.aclose()
    assert connection == module.RuntimeConnection(9, 'http://controller/runtime', 'abc')
    assert [field.name for field in fields(connection)] == ['runtime_id', 'base_url', 'container_id']
    assert status.payload['runtime_status'] == 'running'
    assert calls == ['start' if needs_start else 'mcp']
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert closed == sessions
    stream_session.get.assert_not_called()
    stream_session.commit.assert_not_called()


async def test_expert_control_transactions_use_worker_sessions_and_preserve_terminal_state(monkeypatch):
    from app.exceptions import ConflictError
    loop_thread = threading.get_ident()
    run = SimpleNamespace(id=4, conversation_id=3, engine='expert', status='running',
                          cancel_requested=False, pending_permission_id=None,
                          permission_response=None, metadata_dict={})
    conversation = SimpleNamespace(id=3, title='Expert', opencode_session_id=None,
                                   source_conversation_id=8)
    sessions, closed = [], []
    @contextmanager
    def factory():
        owner = threading.get_ident()
        assert owner != loop_thread
        def check_thread(*args):
            assert threading.get_ident() == owner
        session = SimpleNamespace(get=lambda *args: (check_thread(), conversation)[1], commit=check_thread)
        sessions.append(session)
        try:
            yield session
        finally:
            check_thread()
            closed.append(session)
    stream_session = Mock()
    engine = module.ExpertEngine(session=stream_session, conversation=conversation, run=run)
    engine.runtime_session_factory = factory
    monkeypatch.setattr(module.repository, 'get_owned_run', lambda *args: run)
    monkeypatch.setattr(module.repository, 'lock_run', lambda *args: run)
    monkeypatch.setattr(module, 'assert_can_write', lambda *args: None)
    context = await asyncio.to_thread(engine._load_context, 2, 1)
    assert context == module.ExpertContext('Expert', None, 8)
    await asyncio.to_thread(engine._save_provider_session, 2, 1, 9, 's1')
    assert conversation.opencode_session_id == run.opencode_session_id == 's1'
    assert conversation.opencode_runtime_id == 9
    await asyncio.to_thread(engine._persist_permission, 2, 1, {'permission_id': 'p1', 'title': 'Execute'})
    assert run.status == 'waiting_permission'
    assert await asyncio.to_thread(engine._permission_state, 2, 1, 'p1') == (None, False)
    run.permission_response = 'once'
    run.status = 'running'
    assert await asyncio.to_thread(engine._permission_state, 2, 1, 'other') == (None, False)
    assert await asyncio.to_thread(engine._permission_state, 2, 1, 'p1') == ('once', False)
    run.status = 'failed'
    run.permission_response = None
    run.pending_permission_id = 'p2'
    assert await asyncio.to_thread(engine._permission_state, 2, 1, 'p2') == (None, True)
    with pytest.raises(ConflictError):
        await asyncio.to_thread(engine._persist_permission, 2, 1, {'permission_id': 'p2'})
    assert run.status == 'failed' and run.pending_permission_id == 'p2'
    assert len(sessions) == 8 and closed == sessions
    stream_session.get.assert_not_called()
    stream_session.commit.assert_not_called()
