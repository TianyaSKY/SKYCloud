"""每个文件路由持有并重新鉴权其业务会话测试."""
import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
import httpx
import pytest
from typing_extensions import Self

from app.api.dependencies import get_current_user
from app.exceptions import PermissionDeniedError, register_exception_handlers
from app.features.file import router
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('method,path,kwargs,check', [
    ('POST', '/files', {'files': {'file': ('one.txt', b'abc')}}, 'assert_can_write_locked'),
    ('POST', '/files/batch', {}, 'assert_can_write_locked'),
    ('POST', '/files/preflight', {'json': {'filename': 'one', 'total_size': 3, 'content_hash': 'a'*64}}, 'assert_can_write_locked'),
    ('POST', '/files/multipart/init', {'json': {'filename': 'one', 'total_size': 3}}, 'assert_can_write_locked'),
    ('POST', '/files/multipart/chunk', {'data': {'upload_id': 'opaque', 'chunk_index': '0'}, 'files': {'chunk': ('part', b'abc')}}, 'assert_can_write_locked'),
    ('POST', '/files/multipart/complete', {'json': {'upload_id': 'opaque'}}, 'assert_can_write_locked'),
    ('GET', '/files/multipart/opaque', {}, 'assert_member'),
    ('DELETE', '/files/multipart/opaque', {}, 'assert_can_write_locked'),
    ('GET', '/files/list', {}, 'assert_member'),
    ('PUT', '/files/2', {'json': {'name': 'updated'}}, 'assert_can_write_locked'),
    ('DELETE', '/files/2', {}, 'assert_can_write_locked'),
    ('GET', '/files/2/download', {}, 'assert_member'),
    ('POST', '/files/batch-delete', {'json': {'items': [{'id': 2, 'is_folder': False}]}}, 'assert_can_write_locked'),
    ('POST', '/files/retry_embedding', {'json': {'file_id': 2}}, 'assert_can_write_locked'),
    ('POST', '/files/rebuild_failed_indexes', {}, 'assert_can_write_locked'),
    ('GET', '/files/2', {}, 'assert_member'),
])
async def test_file_worker_authorization_and_cleanup(monkeypatch: pytest.MonkeyPatch, method: str, path: str, kwargs: dict[str, Any], check: str) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened: list[Any] = []
    closed: list[Any] = []

    class Owned:
        def __init__(self) -> None:
            self.thread = threading.get_ident()
            assert self.thread != loop_thread
            opened.append(self)
        def __enter__(self) -> Self:
            return self
        def __exit__(self, *args: Any) -> None:
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
                if dependency.name == 'workspace' and dependency.call is not None:
                    app.dependency_overrides[dependency.call] = lambda: SimpleNamespace(id=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.request(method, path, **kwargs))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 403
    assert len(opened) == 1 and closed == opened

