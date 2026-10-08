"""Share route SQL and response preparation run on an owning worker."""
import asyncio
import threading
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI
from app.api.dependencies import get_current_user
from app.exceptions import ResourceNotFoundError, register_exception_handlers
from app.features.share import router
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('method,path,body,boundary', [
    ('POST', '/share', {'file_id': 2}, 'create_share'),
    ('GET', '/share/my', None, 'get_my_shares'),
    ('DELETE', '/share/2', None, 'cancel_share_for_user'),
    ('GET', '/share/opaque-token', None, 'resolve_shared_file'),
])
async def test_share_worker_session_cleanup_and_responsiveness(monkeypatch, method, path, body, boundary):
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
    def missing(session, *args):
        assert threading.get_ident() == session.thread
        entered.set(); assert release.wait(5)
        raise ResourceNotFoundError('Not found')
    monkeypatch.setattr(router.share_service, boundary, missing)
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
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
