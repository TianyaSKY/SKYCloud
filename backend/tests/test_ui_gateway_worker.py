"""Gateway credential SQL is owned by the endpoint's worker."""
import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.features.workspace import ui_gateway, ui_tickets
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('path,operation', [
    ('/__skycloud/launch?ticket=opaque', 'redeem'),
    ('/__skycloud/renew?ticket=opaque', 'redeem'),
    ('/index.html', 'authenticate'),
])
async def test_gateway_worker_session_closed_on_denied_credential(monkeypatch, path, operation):
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

    def denied(session, raw, host):
        assert threading.get_ident() == session.thread
        assert host == 'runtime.test'
        entered.set()
        assert release.wait(5)
        raise HTTPException(403, 'Permission denied')

    monkeypatch.setattr(ui_tickets, operation, denied)
    app = FastAPI()
    app.include_router(ui_gateway.router)
    app.dependency_overrides[get_session_factory] = lambda: Owned
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://runtime.test') as client:
        pending = asyncio.create_task(client.get('/runtime-ui' + path))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 403
    assert len(opened) == 1 and closed == opened
