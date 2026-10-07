"""Slow synchronous workspace/auth work must leave the ASGI loop responsive."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.api.dependencies import get_current_user, get_current_workspace, require_workspace_role
from app.features.auth import user_service
from app.features.workspace import router as workspace_router
from app.infra.extensions import get_db


@pytest.mark.parametrize('boundary', ['create', 'workspace', 'role', 'profile', 'mcp', 'assistant_list', 'assistant_cancel'])
async def test_slow_sync_boundary_allows_concurrent_http_request(monkeypatch, boundary):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    user = SimpleNamespace(id=1)
    workspace = SimpleNamespace(id=2)

    def slow(result):
        def call(*args, **kwargs):
            # Fail immediately if called on the loop: no deadlock or timing-only assertion.
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(5), 'test failed to release synchronous operation'
            return result
        return call

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: user
    session = SimpleNamespace(get=slow(workspace))
    app.dependency_overrides[get_db] = lambda: session
    app.include_router(workspace_router.router)

    @app.get('/probe')
    async def probe():
        assert not release.is_set()
        return {'responsive': True}

    if boundary in {'assistant_list', 'assistant_cancel'}:
        from app.features.assistant import router as assistant_router
        app.include_router(assistant_router.router)
        app.dependency_overrides[get_current_workspace] = lambda: workspace
        if boundary == 'assistant_list':
            monkeypatch.setattr(assistant_router.service, 'list_user_conversations', slow([]))
            method, path, kwargs = 'GET', '/assistant/conversations', {}
        else:
            monkeypatch.setattr(assistant_router.service, 'request_cancel',
                                slow(SimpleNamespace(to_dict=lambda: {'id': 2, 'cancel_requested': True})))
            async def notify_cancel(run_id):
                assert threading.get_ident() == loop_thread
            monkeypatch.setattr(assistant_router.service.run_registry, 'request_cancel', notify_cancel)
            method, path, kwargs = 'POST', '/assistant/runs/2/cancel', {}
    elif boundary == 'create':
        monkeypatch.setattr(workspace_router.workspace_service, 'create_workspace',
                            slow(SimpleNamespace(to_dict=lambda: dict(id=2, name='Thread pool', description=None, owner_id=1,
                                created_at=None, updated_at=None, container_id=None, status='stopped', error_message=None))))
        method, path, kwargs = 'POST', '/workspace', {'json': {'name': 'Thread pool'}}
    elif boundary == 'workspace':
        monkeypatch.setattr('app.api.dependencies.assert_member', lambda *args: None)
        @app.get('/slow')
        async def read_workspace(value=Depends(get_current_workspace)):
            return {'id': value.id}
        method, path, kwargs = 'GET', '/slow', {'headers': {'X-Workspace-Id': '2'}}
    elif boundary == 'role':
        app.dependency_overrides[get_current_workspace] = lambda: workspace
        monkeypatch.setattr('app.features.workspace.permissions.get_member_role', slow('editor'))
        @app.get('/slow')
        async def read_role(value=Depends(require_workspace_role('editor'))):
            return {'id': value.id}
        method, path, kwargs = 'GET', '/slow', {}
    elif boundary == 'mcp':
        import jwt
        from fastapi.security import HTTPAuthorizationCredentials
        from app.infra.extensions import SECRET_KEY
        monkeypatch.setattr('app.mcp.token_service.get_active_mcp_token', slow(object()))
        async def cached_user(*args):
            return user
        monkeypatch.setattr(user_service, 'get_user', cached_user)
        credentials = HTTPAuthorizationCredentials(scheme='Bearer',
            credentials=jwt.encode({'sub': '1', 'type': 'mcp'}, SECRET_KEY, algorithm='HS256'))
        @app.get('/slow')
        async def read_mcp_user():
            value = await get_current_user(credentials, session)
            return {'id': value.id}
        method, path, kwargs = 'GET', '/slow', {}
    else:
        monkeypatch.setattr(user_service, '_get_user_data', slow({'id': 1}))
        monkeypatch.setattr(user_service.User, 'from_cache', lambda data: user)
        @app.get('/slow')
        async def read_profile():
            value = await user_service.get_user(session, 1)
            return {'id': value.id}
        method, path, kwargs = 'GET', '/slow', {}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        request = asyncio.create_task(client.request(method, path, **kwargs))
        try:
            assert await asyncio.to_thread(entered.wait, 2), 'synchronous boundary did not start'
            response = await asyncio.wait_for(client.get('/probe'), timeout=1)
            assert response.json() == {'responsive': True}
            assert not request.done()
        finally:
            release.set()
            response = await request
        assert response.status_code in (200, 201), response.text


def test_http_create_workspace_preserves_description_and_validation(session, test_user):
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(workspace_router.router)
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        response = client.post('/workspace', json={'name': '  Created over HTTP  ', 'description': 'Description'})
        assert response.status_code == 201, response.text
        assert response.json()['data']['name'] == 'Created over HTTP'
        assert response.json()['data']['description'] == 'Description'
        assert client.post('/workspace', json={'name': 'Without description'}).status_code == 201
        assert client.post('/workspace', json={'name': '   '}).status_code == 422
        assert client.post('/workspace', json={'name': 'n' * 121}).status_code == 422
        assert client.post('/workspace', json={'name': 'Valid', 'description': 'd' * 513}).status_code == 422
