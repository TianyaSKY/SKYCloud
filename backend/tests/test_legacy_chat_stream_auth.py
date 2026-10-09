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


@pytest.mark.parametrize('cause',['initial-auth','quiet-role','eof'])
@pytest.mark.parametrize('close_error',[RuntimeError,asyncio.CancelledError])
async def test_legacy_iterator_close_fault_preserves_authorization_or_eof(monkeypatch,cause,close_error):
    from app.exceptions import AuthenticationError
    from loguru import logger
    calls,checks,logs=[],[],[]
    monkeypatch.setattr(logger,'error',lambda message,*args:logs.append(message.format(*args)))
    def check(*args):
        checks.append(True)
        if cause=='initial-auth':raise AuthenticationError('fixed-auth')
        if cause=='quiet-role' and len(checks)>1:raise PermissionDeniedError('fixed-role')
    monkeypatch.setattr(guard,'require_membership',check)
    monkeypatch.setattr(guard,'POLL_SECONDS',0.01)
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):
            try:
                if cause=='eof':raise StopAsyncIteration
                await asyncio.Event().wait()
            finally:calls.append('read-finished')
        async def aclose(self):
            calls.append('close');raise close_error('private-provider-credential')
    async def collect():return [part async for part in guard.authorized_events(Iterator(),None,2,1)]
    if cause=='eof':assert await asyncio.wait_for(collect(),1)==[]
    else:
        expected=AuthenticationError if cause=='initial-auth' else PermissionDeniedError
        with pytest.raises(expected):await asyncio.wait_for(collect(),1)
    assert calls==(['close'] if cause=='initial-auth' else ['read-finished','close'])
    assert logs==[f'Stream iterator close failed (error_type={close_error.__name__})']


@pytest.mark.parametrize('mode',['repeat','anyio'])
async def test_legacy_cancel_drains_reader_before_close(monkeypatch,mode):
    import anyio
    started,cleaning,release=(asyncio.Event() for _ in range(3))
    calls,scopes=[],[]
    monkeypatch.setattr(guard,'require_membership',lambda *args:None)
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):
            try:started.set();await asyncio.Event().wait()
            finally:cleaning.set();await release.wait();calls.append('read-finished')
        async def aclose(self):
            assert calls==['read-finished'];calls.append('close')
    async def collect():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async for _ in guard.authorized_events(Iterator(),None,2,1):pass
        assert mode=='anyio' and scope.cancelled_caught
    pending=asyncio.create_task(collect())
    try:
        await started.wait()
        if mode=='anyio':scopes[0].cancel()
        else:pending.cancel()
        await asyncio.wait_for(cleaning.wait(),1)
        if mode=='repeat':pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done(),'reader cleanup was interrupted'
    finally:
        release.set()
        if mode=='repeat':
            with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
        else:await asyncio.wait_for(pending,1)
    assert calls==['read-finished','close']
