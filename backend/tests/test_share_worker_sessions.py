"""分享路由 SQL 与响应准备在所属 worker 上运行测试."""
import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.dependencies import get_current_user
from app.exceptions import ResourceNotFoundError, register_exception_handlers
from app.features.share import router
from app.infra.extensions import get_session_factory


CASES: list[tuple[str, str, dict[str, int] | None, str]] = [
    ('POST', '/share', {'file_id': 2}, 'create_share'),
    ('GET', '/share/my', None, 'get_my_shares'),
    ('DELETE', '/share/2', None, 'cancel_share_for_user'),
    ('GET', '/share/opaque-token', None, 'resolve_shared_file'),
]


@pytest.mark.parametrize('method,path,body,boundary', CASES)
async def test_share_worker_session_cleanup_and_responsiveness(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, int] | None,
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

    def missing(session: Any, *args: object) -> Any:
        assert threading.get_ident() == session.thread
        entered.set()
        assert release.wait(5)
        raise ResourceNotFoundError('Not found')

    monkeypatch.setattr(router.share_service, boundary, missing)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: Owned
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.request(method, path, json=body))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 404
    assert len(opened) == 1 and closed == opened
