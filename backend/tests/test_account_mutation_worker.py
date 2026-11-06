"""账户授权与业务 SQL 共享路由 worker 的 session 测试."""
import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.dependencies import get_current_user, require_admin
from app.exceptions import PermissionDeniedError, register_exception_handlers
from app.features.auth import router, user_service
from app.infra.extensions import get_session_factory


CASES: list[tuple[str, str, dict[str, str] | None, str]] = [
    ('POST', '/users', {'username': 'new', 'password': 'password'}, 'authorize_account_mutation'),
    ('PUT', '/users/2', {'username': 'updated'}, 'authorize_account_mutation'),
    ('PUT', '/users/2/password', {'old_password': 'old', 'new_password': 'new'}, 'authorize_account_mutation'),
    ('DELETE', '/users/2', None, 'authorize_account_mutation'),
    ('POST', '/auth/login', {'username': 'actor', 'password': 'password'}, 'login'),
    ('POST', '/auth/register', {'username': 'new', 'password': 'password'}, 'register_user'),
    ('POST', '/auth/refresh', None, 'refresh'),
    ('GET', '/auth/mcp-token', None, 'get_mcp_token'),
    ('POST', '/auth/mcp-token/refresh', None, 'refresh_mcp_token'),
]


@pytest.mark.parametrize('method,path,body,boundary', CASES)
async def test_account_mutation_worker_owns_authorization_session(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, str] | None,
    boundary: str,
) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened: list[Any] = []
    closed: list[Any] = []

    class Owned:
        def __init__(self) -> None:
            self.thread = threading.get_ident()
            assert self.thread != loop_thread
            opened.append(self)

        def __enter__(self) -> "Owned":
            return self

        def __exit__(self, *args: object) -> None:
            assert threading.get_ident() == self.thread
            closed.append(self)

    def denied(session: Any, *args: object, **kwargs: object) -> Any:
        assert threading.get_ident() == session.thread
        entered.set()
        assert release.wait(5)
        raise PermissionDeniedError('Permission denied')

    owner: object = (
        user_service
        if boundary == 'authorize_account_mutation'
        else router.refresh_sessions
        if boundary == 'refresh'
        else router.auth_service
    )
    monkeypatch.setattr(owner, boundary, denied)

    def _mock_check_origin(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(router.refresh_sessions, 'check_origin', _mock_check_origin)
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
