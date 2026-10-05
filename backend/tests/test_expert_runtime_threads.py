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
    engine = module.ExpertEngine(session=stream_session, conversation=Mock(), run=Mock())
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
    assert connection == module.RuntimeConnection(9, 'http://controller/runtime')
    assert [field.name for field in fields(connection)] == ['runtime_id', 'base_url']
    assert status.payload['runtime_status'] == 'running'
    assert calls == ['start' if needs_start else 'mcp']
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert closed == sessions
    stream_session.get.assert_not_called()
    stream_session.commit.assert_not_called()
