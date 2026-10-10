"""Gateway credential SQL is owned by the endpoint's worker."""
import asyncio
import threading
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.features.workspace import ui_gateway, ui_tickets
from app.infra.extensions import get_session_factory


@pytest.fixture
def held_ui_worker():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    closed = []

    class Owned:
        def __enter__(self):
            self.thread = threading.get_ident()
            return self

        def __exit__(self, *args):
            assert threading.get_ident() == self.thread
            closed.append(self)

    def hold(fault):
        entered.set()
        try:
            assert release.wait(5)
            if fault:
                raise RuntimeError('private-ui-worker-error')
        finally:
            finished.set()

    payload = {'origin': 'http://runtime.test', 'expires_at': time.time() + 60,
               'container_id': 'a' * 64, 'user_id': 1}
    return entered, release, finished, closed, Owned, hold, payload


@pytest.mark.parametrize('stage', ['launch', 'renew', 'admission', 'body', 'headers', 'output'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cancelled_ui_request_drains_worker_before_upstream_close(monkeypatch, held_ui_worker, stage, fault):
    entered, release, finished, closed, Owned, hold, payload = held_ui_worker
    calls, clients = 0, []
    target = {'admission': 1, 'body': 2, 'headers': 3, 'output': 4}.get(stage)

    def authenticate(session, raw, host):
        nonlocal calls
        assert threading.get_ident() == session.thread
        calls += 1
        if calls == target or stage in {'launch', 'renew'}:
            hold(fault)
        return ('opaque', payload) if stage in {'launch', 'renew'} else payload

    original_client = httpx.AsyncClient
    def provider(**kwargs):
        client = original_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'ui-body')))
        clients.append(client)
        return client

    monkeypatch.setattr(ui_tickets, 'authenticate', authenticate)
    monkeypatch.setattr(ui_tickets, 'redeem', authenticate)
    monkeypatch.setattr(ui_gateway.httpx, 'AsyncClient', provider)
    app = FastAPI()
    app.include_router(ui_gateway.router)
    app.dependency_overrides[get_session_factory] = lambda: Owned
    path = '/runtime-ui/__skycloud/' + stage + '?ticket=opaque' if stage in {'launch', 'renew'} else '/runtime-ui/index.html'
    async with original_client(transport=httpx.ASGITransport(app), base_url='http://runtime.test') as client:
        pending = asyncio.create_task(client.get(path, headers={'Cookie': ui_tickets.COOKIE + '=opaque'}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            previous = len(closed)
            pending.cancel('caller-stop')
            await asyncio.sleep(.01)
            pending.cancel('repeat-stop')
            await asyncio.sleep(.01)
            assert not pending.done() and not finished.is_set()
            assert len(closed) == previous and all(not client.is_closed for client in clients)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending, 2)
            assert await asyncio.to_thread(finished.wait, 2)
    if stage != 'output':
        assert str(error.value) == 'caller-stop'
    assert len(closed) == calls and all(client.is_closed for client in clients)


@pytest.mark.parametrize('stage', ['admission', 'guard'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cancelled_terminal_drains_auth_before_socket_and_slot_release(monkeypatch, held_ui_worker, stage, fault):
    from starlette.websockets import WebSocket
    from runtime_controller.socket_relay import SocketSlots
    entered, release, finished, closed, Owned, hold, payload = held_ui_worker
    calls, upstream_closed = 0, []

    def authenticate(session, raw, host):
        nonlocal calls
        calls += 1
        if calls == (1 if stage == 'admission' else 2):
            hold(fault)
        return payload

    class Peer:
        async def send(self, data): pass
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield ''

    @asynccontextmanager
    async def connect(*args):
        try:
            yield Peer()
        finally:
            upstream_closed.append(True)

    connected = False
    async def receive():
        nonlocal connected
        if not connected:
            connected = True
            return {'type': 'websocket.connect'}
        await asyncio.Event().wait()
    async def send(message): pass
    websocket = WebSocket({'type': 'websocket', 'path': '/', 'query_string': b'',
        'headers': [(b'host', b'runtime.test'), (b'origin', b'http://runtime.test'),
                    (b'cookie', (ui_tickets.COOKIE + '=opaque').encode())]}, receive, send)
    slots = SocketSlots()
    monkeypatch.setattr(ui_gateway, 'socket_slots', slots)
    monkeypatch.setattr(ui_gateway, 'direct_connect', connect)
    monkeypatch.setattr(ui_gateway, 'RECHECK_SECONDS', .01)
    monkeypatch.setattr(ui_tickets, 'authenticate', authenticate)
    pending = asyncio.create_task(ui_gateway.terminal('pty/owned/connect', websocket, Owned))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        previous = len(closed)
        pending.cancel('caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and not finished.is_set()
        assert len(closed) == previous and not upstream_closed
        assert slots.counts == ({1: 1} if stage == 'guard' else {})
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert await asyncio.to_thread(finished.wait, 2)
    assert len(closed) == calls and not slots.counts
    assert upstream_closed == ([True] if stage == 'guard' else [])


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
