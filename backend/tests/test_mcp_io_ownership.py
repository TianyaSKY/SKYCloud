"""MCP 认证/搜索在 worker 上持有其会话, 而非事件循环上测试."""
import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.exceptions import PermissionDeniedError
from app.mcp import server, authentication
from app.features.file import search_service
from app.mcp.context import McpRequestContext, get_optional_mcp_context, set_mcp_context, reset_mcp_context


async def test_slow_mcp_auth_allows_another_http_request_and_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    closed: list[bool] = []
    class Session:
        def __init__(self) -> None:
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
        def close(self) -> None:
            assert threading.get_ident() == self.owner
            closed.append(True)
    def resolve(session: Session, token: str, workspace_header: str) -> McpRequestContext:
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
    async def who() -> dict[str, Any]:
        assert closed == [True]
        context = get_optional_mcp_context()
        assert context is not None
        return {'user': context.user_id, 'workspace': context.workspace_id}
    @app.get('/probe')
    async def probe() -> bool:
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
async def test_mcp_search_worker_owned_session_scope_and_revocation(monkeypatch: pytest.MonkeyPatch, mode: str, revoked: bool) -> None:
    loop_thread = threading.get_ident()
    created: list[Any] = []
    closed: list[Any] = []
    members: list[tuple[int, int]] = []
    class Session:
        def __init__(self) -> None:
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            created.append(self)
        def close(self) -> None:
            assert threading.get_ident() == self.owner
            closed.append(self)
    def membership(session: Any, workspace_id: int, user_id: int) -> None:
        assert threading.get_ident() == session.owner
        members.append((workspace_id, user_id))
        if revoked:
            raise PermissionDeniedError('Workspace access revoked')
    def fetch(session: Any, workspace_id: int, *args: Any) -> dict[str, Any]:
        assert threading.get_ident() == session.owner and workspace_id == 2
        return {'items': [{'id': 9}], 'total': 1}
    monkeypatch.setattr(server, 'SessionLocal', Session)
    monkeypatch.setattr(search_service, 'assert_member', membership)
    monkeypatch.setattr(search_service, '_search_files_fuzzy', fetch)
    monkeypatch.setattr(search_service, '_search_files_vector_by_embedding', fetch)
    monkeypatch.setattr(search_service, 'embedding_desc', AsyncMock(return_value=[0.1]))
    def fake_cfg() -> dict[str, Any]:
        return {}
    monkeypatch.setattr(search_service, 'get_embedding_model_config', fake_cfg)
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


async def test_text_read_closes_session_before_touching_object_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """鉴权必须持有其 Session; 存储 I/O 绝不能在其内部运行."""
    state: dict[str, Any] = {'closed': False, 'storage_saw_closed': None}
    class Session:
        def close(self) -> None:
            state['closed'] = True
    class SessionFactory:
        def __call__(self) -> Session:
            return Session()
    file = SimpleNamespace(name='read.txt', mime_type='text/plain', file_path='object',
                           get_abs_path=lambda: None)
    def file_exists(name: str) -> bool:
        state['storage_saw_closed'] = state['closed']
        return False
    def fake_size(_name: str) -> int:
        return 0
    def fake_prefix(_name: str, _limit: int) -> bytes:
        return b''
    storage = SimpleNamespace(file_exists=file_exists, get_file_size=fake_size,
                              read_file_prefix=fake_prefix)
    def fake_auth_file(*_args: Any) -> Any:
        return file
    monkeypatch.setattr(server, 'SessionLocal', SessionFactory())
    monkeypatch.setattr(server, 'require_read_context', lambda: None)
    monkeypatch.setattr(server, 'get_authorized_file', fake_auth_file)
    monkeypatch.setattr(server, 'get_storage_client', lambda: storage)
    monkeypatch.setattr(server, 'is_mcp_request_active', lambda: False)


    payload = json.loads(await server.read_file_content(1))

    assert state['closed'] is True
    assert state['storage_saw_closed'] is True, 'object storage was read while the Session was still open'
    assert payload == {'error': 'File not found on server', 'success': False} or 'error' in payload

