"""MCP authentication/search own their sessions on workers, not on the loop."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.exceptions import PermissionDeniedError
from app.mcp import server, authentication
from app.features.file import search_service
from app.mcp.context import McpRequestContext, get_optional_mcp_context, set_mcp_context, reset_mcp_context


async def test_slow_mcp_auth_allows_another_http_request_and_closes_session(monkeypatch):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    closed = []
    class Session:
        def __init__(self):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
        def close(self):
            assert threading.get_ident() == self.owner
            closed.append(True)
    def resolve(session, token, workspace_header):
        assert threading.get_ident() == session.owner
        assert token == 'synthetic' and workspace_header == '2'
        entered.set()
        assert release.wait(5)
        return McpRequestContext(1, 2, 'viewer')
    monkeypatch.setattr(authentication, 'SessionLocal', Session)
    monkeypatch.setattr(server.JWTAuthMiddleware, '_resolve_context', staticmethod(resolve))
    app = FastAPI()
    app.add_middleware(server.JWTAuthMiddleware)
    @app.get('/who')
    async def who():
        assert closed == [True]
        context = get_optional_mcp_context()
        return {'user': context.user_id, 'workspace': context.workspace_id}
    @app.get('/probe')
    async def probe():
        assert not release.is_set()
        assert get_optional_mcp_context() is None
        return True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        operation = asyncio.create_task(client.get('/who', headers={
            'Authorization': 'Bearer synthetic', 'X-SKYCloud-Workspace-Id': '2'}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await asyncio.wait_for(client.get('/probe'), 1)).json() is True
            assert not operation.done()
        finally:
            release.set()
            response = await operation
    assert response.json() == {'user': 1, 'workspace': 2}
    assert closed == [True] and get_optional_mcp_context() is None


@pytest.mark.parametrize('mode', ['fuzzy', 'vector'])
@pytest.mark.parametrize('revoked', [False, True])
async def test_mcp_search_worker_owned_session_scope_and_revocation(monkeypatch, mode, revoked):
    loop_thread = threading.get_ident()
    created, closed, members = [], [], []
    class Session:
        def __init__(self):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            created.append(self)
        def close(self):
            assert threading.get_ident() == self.owner
            closed.append(self)
    def membership(session, workspace_id, user_id):
        assert threading.get_ident() == session.owner
        members.append((workspace_id, user_id))
        if revoked:
            raise PermissionDeniedError('Workspace access revoked')
    def fetch(session, workspace_id, *args):
        assert threading.get_ident() == session.owner and workspace_id == 2
        return {'items': [{'id': 9}], 'total': 1}
    monkeypatch.setattr(server, 'SessionLocal', Session)
    monkeypatch.setattr(search_service, 'assert_member', membership)
    monkeypatch.setattr(search_service, '_search_files_fuzzy', fetch)
    monkeypatch.setattr(search_service, '_search_files_vector_by_embedding', fetch)
    monkeypatch.setattr(search_service, 'embedding_desc', AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(search_service, 'get_embedding_model_config', lambda: {})
    token = set_mcp_context(McpRequestContext(1, 2, 'viewer'))
    try:
        if revoked:
            with pytest.raises(PermissionDeniedError, match='revoked'):
                await server.search_files('query', search_type=mode)
        else:
            result = json.loads(await server.search_files('query', search_type=mode))
            assert result['items'] == [{'id': 9}]
    finally:
        reset_mcp_context(token)
    expected = 2 if mode == 'vector' and not revoked else 1
    assert len(created) == expected and closed == created
    assert members == [(2, 1)] * expected
