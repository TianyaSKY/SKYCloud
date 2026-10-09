"""Legacy SSE rechecks membership without blocking its loop or leaking late data."""
import asyncio
import threading
import pytest
from app.exceptions import PermissionDeniedError
from app.features.chat import repository, stream_authorization as guard


@pytest.mark.parametrize('revoke_at', ['initial','event','quiet'])
async def test_legacy_stream_rechecks_current_role_and_closes_upstream(monkeypatch, revoke_at):
    loop_thread = threading.get_ident()
    revoked, started, closed = [revoke_at == 'initial'], [], []
    entered = asyncio.Event()
    sessions = []
    class Owned:
        def __init__(self):
            self.thread = threading.get_ident()
            assert self.thread != loop_thread
            self.closed = False; sessions.append(self)
        def close(self):
            assert threading.get_ident() == self.thread
            self.closed = True
    def member(session, workspace, user):
        assert workspace == 2 and user == 1
        assert threading.get_ident() == session.thread
        if revoked[0]: raise PermissionDeniedError('revoked')
    monkeypatch.setattr(repository,'assert_member',member)
    monkeypatch.setattr(guard,'POLL_SECONDS',0.01)
    async def events():
        started.append(True)
        try:
            if revoke_at == 'quiet':
                entered.set(); await asyncio.Event().wait()
            yield 'first'
            revoked[0] = True
            yield 'private-late-data'
        finally: closed.append(True)
    received=[]
    async def collect():
        async for event in guard.authorized_events(events(),Owned,2,1): received.append(event)
    task=asyncio.create_task(collect())
    if revoke_at == 'quiet':
        await asyncio.wait_for(entered.wait(),1)
        revoked[0] = True
    with pytest.raises(PermissionDeniedError): await asyncio.wait_for(task,2)
    assert received == (['first'] if revoke_at == 'event' else [])
    assert closed == ([True] if started else [])
    assert sessions and all(session.closed for session in sessions)


async def test_legacy_stream_disconnect_drains_pending_iterator(monkeypatch):
    entered,closed = asyncio.Event(),asyncio.Event()
    monkeypatch.setattr(guard,'require_membership',lambda *args:None)
    async def events():
        try:
            entered.set(); await asyncio.Event().wait(); yield 'never'
        finally:closed.set()
    task=asyncio.create_task(anext(guard.authorized_events(events(),None,2,1)))
    await asyncio.wait_for(entered.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert closed.is_set()


async def test_legacy_http_membership_failure_stays_safe_after_headers(monkeypatch):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user,get_current_workspace
    from app.infra.extensions import get_session_factory
    from app.features.chat import router
    private='private-db-password-and-url'
    called,logged=[],[]
    def broken_factory():raise RuntimeError(private)
    async def model(*args):
        called.append(True);yield 'never'
    monkeypatch.setattr(router,'generate_chat_events',model)
    monkeypatch.setattr(router.logger,'error',lambda message,*args:logged.append(message.format(*args)))
    app=FastAPI();app.include_router(router.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=1)
    app.dependency_overrides[get_current_workspace]=lambda:SimpleNamespace(id=2)
    app.dependency_overrides[get_session_factory]=lambda:broken_factory
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        response=await client.post('/chat',json={'query':'question'})
    assert response.status_code==200 and 'data: ' in response.text
    assert called==[] and logged and private not in response.text+str(logged)
