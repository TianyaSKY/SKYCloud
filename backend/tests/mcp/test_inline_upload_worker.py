"""Inline adaptation runs off-loop before a separate worker-owned SQL unit."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.mcp import server
from app.mcp.context import McpRequestContext, set_mcp_context, reset_mcp_context


async def test_blocked_adaptation_leaves_loop_free_and_keeps_sql_session_on_worker(monkeypatch):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    sessions, closed = [], []
    original = server._inline_upload_adapter
    def adapt(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        return original(*args)
    class Session:
        def __init__(self):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            sessions.append(self)
        def close(self):
            assert threading.get_ident() == self.owner
            closed.append(self)
    def authorize(session, context):
        assert threading.get_ident() == session.owner
        assert context.workspace_id == 2 and context.user_id == 1
    def upload(session, workspace, user, adapter, parent):
        assert threading.get_ident() == session.owner
        assert adapter.filename == 'note.txt' and workspace == 2 and user == 1
        return SimpleNamespace(to_dict=lambda: {'id':7})
    monkeypatch.setattr(server, '_inline_upload_adapter', adapt)
    monkeypatch.setattr(server, 'SessionLocal', Session)
    monkeypatch.setattr(server, '_assert_current_write_permission', authorize)
    monkeypatch.setattr(server.file_service, 'create_uploaded_file', upload)
    token = set_mcp_context(McpRequestContext(1,2,'editor'))
    task = asyncio.create_task(server.upload_file('note.txt','hello'))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done() and not sessions
        async def probe():
            assert not release.is_set()
            return True
        assert await asyncio.wait_for(probe(), 1)
    finally:
        release.set()
        try:
            result = json.loads(await task)
        finally:
            reset_mcp_context(token)
    assert result['success'] and result['file']['id'] == 7
    assert len(sessions) == 1 and closed == sessions


async def test_viewer_is_rejected_before_any_inline_adaptation(monkeypatch):
    adapt = MagicMock(side_effect=AssertionError('read-only input must not be adapted'))
    monkeypatch.setattr(server, '_inline_upload_adapter', adapt)
    token = set_mcp_context(McpRequestContext(1,2,'viewer'))
    try:
        with pytest.raises(PermissionError, match='read-only'):
            await server.upload_file('note.txt','hello')
    finally:
        reset_mcp_context(token)
    adapt.assert_not_called()
