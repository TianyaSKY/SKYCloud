"""Model authentication sessions belong to their dependency worker."""
import asyncio
import threading
import httpx
import pytest
from fastapi import Depends, FastAPI
from app.features.runtime_llm import router
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('allowed', [True, False])
async def test_model_credential_session_owner_cleanup_and_loop(monkeypatch, allowed):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened, closed = [], []
    class Owned:
        def __init__(self):
            self.thread = threading.get_ident()
            assert self.thread != loop_thread
            opened.append(self)
        def __enter__(self): return self
        def __exit__(self, *args):
            assert threading.get_ident() == self.thread
            closed.append(self)
    def validate(session, raw):
        assert threading.get_ident() == session.thread and raw == 'opaque'
        entered.set(); assert release.wait(5)
        return {'sub':'1'} if allowed else None
    monkeypatch.setattr(router.tokens, 'validate_token', validate)
    app = FastAPI()
    app.dependency_overrides[get_session_factory] = lambda: Owned
    @app.get('/probe')
    async def probe(credential=Depends(router.current_credential)):
        assert closed == opened
        return credential['sub']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.get('/probe', headers={'Authorization':'Bearer opaque'}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally: release.set()
        assert (await pending).status_code == (200 if allowed else 401)
    assert len(opened) == 1 and closed == opened
