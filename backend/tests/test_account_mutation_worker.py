"""Account authorization and business SQL share the route worker's session."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.dependencies import get_current_user, require_admin
from app.exceptions import PermissionDeniedError, register_exception_handlers
from app.features.auth import router, user_service
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('method,path,body,boundary', [
    ('POST', '/users', {'username': 'new', 'password': 'password'}, 'authorize_account_mutation'),
    ('PUT', '/users/2', {'username': 'updated'}, 'authorize_account_mutation'),
    ('PUT', '/users/2/password', {'old_password': 'old', 'new_password': 'new'}, 'authorize_account_mutation'),
    ('DELETE', '/users/2', None, 'authorize_account_mutation'),
    ('POST', '/auth/login', {'username': 'actor', 'password': 'password'}, 'login'),
    ('POST', '/auth/register', {'username': 'new', 'password': 'password'}, 'register_user'),
    ('POST', '/auth/refresh', None, 'refresh'),
    ('GET', '/auth/mcp-token', None, 'get_mcp_token'),
    ('POST', '/auth/mcp-token/refresh', None, 'refresh_mcp_token'),
])
async def test_account_mutation_worker_owns_authorization_session(monkeypatch, method, path, body, boundary):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened, closed = [], []

    class Owned:
        def __init__(self):
            self.thread = threading.get_ident()
            assert self.thread != loop_thread
            opened.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            assert threading.get_ident() == self.thread
            closed.append(self)

    def denied(session, *args, **kwargs):
        assert threading.get_ident() == session.thread
        entered.set()
        assert release.wait(5)
        raise PermissionDeniedError('Permission denied')

    owner = user_service if boundary == 'authorize_account_mutation' else router.refresh_sessions if boundary == 'refresh' else router.auth_service
    monkeypatch.setattr(owner, boundary, denied)
    monkeypatch.setattr(router.refresh_sessions, 'check_origin', lambda *args, **kwargs: None)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, role='admin')
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, role='admin')
    app.dependency_overrides[get_session_factory] = lambda: Owned
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.request(method, path, json=body))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 403
    assert len(opened) == 1 and closed == opened
