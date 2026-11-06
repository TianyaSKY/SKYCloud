"""每个文件夹路由持有并重新鉴权其业务会话测试."""
import asyncio
import threading
from types import SimpleNamespace
from typing import Any
import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from app.api.dependencies import get_current_user
from app.exceptions import PermissionDeniedError, register_exception_handlers
from app.features.folder import router
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('method,path,body,check', [
    ('POST', '/folder', {'name': 'child'}, 'assert_can_write_locked'),
    ('PUT', '/folder/2', {'name': 'updated'}, 'assert_can_write_locked'),
    ('DELETE', '/folder/2', None, 'assert_can_write_locked'),
    ('POST', '/folder/organize', None, 'assert_can_write_locked'),
    ('GET', '/folder/root_id', None, 'assert_member'),
    ('GET', '/folder/all', None, 'assert_member'),
    ('GET', '/folder/2', None, 'assert_member'),
])
async def test_folder_worker_authorization_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    check: str,
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
        def __enter__(self) -> 'Owned':
            return self
        def __exit__(self, *args: object) -> None:
            assert threading.get_ident() == self.thread
            closed.append(self)

    def denied(session: Any, workspace_id: int, user_id: int) -> None:
        assert threading.get_ident() == session.thread
        assert (workspace_id, user_id) == (1, 1)
        entered.set()
        assert release.wait(5)
        raise PermissionDeniedError('Permission denied')

    monkeypatch.setattr(router, check, denied)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: Owned
    for route in router.router.routes:
        if isinstance(route, APIRoute):
            for dependency in route.dependant.dependencies:
                if dependency.name == 'workspace':
                    assert dependency.call is not None
                    app.dependency_overrides[dependency.call] = lambda: SimpleNamespace(id=1)
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
