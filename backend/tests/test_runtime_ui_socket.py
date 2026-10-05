"""WebSocket scoping, transparent frames, quotas and idle revocation."""
import asyncio
import time
import threading
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect

from tests.test_runtime_ui import ui, launch
from app.features.workspace import ui_gateway, ui_tickets
from runtime_controller.socket_relay import SocketSlots, MAX_INPUT


class TrackingSlots(SocketSlots):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.released = threading.Event()
    def acquire(self, owner):
        self.released.clear()
        return super().acquire(owner)
    def release(self, owner):
        super().release(owner)
        self.released.set()


class EchoSocket:
    def __init__(self):
        self.queue = asyncio.Queue(); self.closed = False; self.sent = []
    async def send(self, message):
        self.sent.append(message); await self.queue.put(message)
    async def __aiter__(self):
        while True:
            yield await self.queue.get()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        self.closed = True


@pytest.fixture
def socket_setup(ui, session, test_user, test_workspace, monkeypatch):
    host, raw, _ = launch(ui, test_user, test_workspace)
    headers = {'Host': host, 'Origin': 'http://' + host,
               'Cookie': ui_tickets.COOKIE + '=' + raw}
    peers = []; requests = []
    def connect(url, headers):
        peer = EchoSocket(); peers.append(peer); requests.append((url, headers)); return peer
    monkeypatch.setattr(ui_gateway, 'socket_slots', TrackingSlots())
    with patch.object(ui_gateway, 'reauthenticate', side_effect=lambda raw, host:
                      ui_tickets.authenticate(session, raw, host)), \
         patch.object(ui_gateway, 'direct_connect', side_effect=connect):
        yield ui[0], headers, peers, requests


@pytest.mark.parametrize('prefix, directory', [('pty', 'directory'), ('api/pty', 'location%5Bdirectory%5D')])
def test_socket_transparent_text_and_bytes_and_cleanup(socket_setup, prefix, directory):
    client, headers, peers, requests = socket_setup
    with client.websocket_connect(f'/runtime-ui/{prefix}/pty_42/connect?{directory}=%2Fworkspace&cursor=0', headers={
        **headers, 'Authorization': 'Bearer caller', 'X-Runtime-Controller-Token': 'attacker'}) as socket:
        socket.send_text('echo hello\r'); assert socket.receive_text() == 'echo hello\r'
        socket.send_bytes(b'\x00\xff\x04'); assert socket.receive_bytes() == b'\x00\xff\x04'
    assert ui_gateway.socket_slots.released.wait(2)
    assert peers[0].closed and not ui_gateway.socket_slots.counts
    url, forwarded = requests[0]
    assert '/runtimes/' + 'a' * 64 + f'/ws/{prefix}/pty_42/connect?' in url
    assert directory + '=%2Fworkspace' in url
    assert 'authorization' not in {key.lower() for key in forwarded}
    assert 'cookie' not in {key.lower() for key in forwarded}
    assert forwarded['X-Runtime-Controller-Token'] != 'attacker'


@pytest.mark.parametrize('kind', ['no-cookie', 'origin', 'host', 'path', 'query-jwt'])
def test_socket_rejects_scope_violations(socket_setup, kind):
    client, headers, peers, _ = socket_setup
    path = '/runtime-ui/pty/pty_42/connect'
    if kind == 'no-cookie':
        headers = {**headers, 'Cookie': ''}
    elif kind == 'origin':
        headers = {**headers, 'Origin': 'http://r999.skycloud.localhost:8081'}
    elif kind == 'host':
        headers = {**headers, 'Host': 'r999.skycloud.localhost:8081'}
    elif kind == 'path':
        path = '/runtime-ui/global/health'
    else:
        path += '?token=full-web-jwt'
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, headers=headers):
            pass
    assert exc.value.code == 1008
    assert not peers and not ui_gateway.socket_slots.counts


def test_socket_rejects_oversize_before_upstream_send(socket_setup):
    client, headers, peers, _ = socket_setup
    with client.websocket_connect('/runtime-ui/pty/pty_42/connect', headers=headers) as socket:
        socket.send_bytes(b'x' * (MAX_INPUT + 1))
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_bytes()
        assert exc.value.code == 1009
    assert ui_gateway.socket_slots.released.wait(2)
    assert peers[0].closed and not peers[0].sent


@pytest.mark.parametrize('end', ['revoke', 'expire'])
def test_idle_socket_is_closed_on_revocation_or_expiry(socket_setup, monkeypatch, end):
    client, headers, peers, _ = socket_setup
    original = ui_gateway.reauthenticate
    calls = 0
    def validate(raw, host):
        nonlocal calls
        calls += 1
        if end == 'revoke' and calls > 1:
            raise HTTPException(401, 'revoked')
        payload = original(raw, host)
        if end == 'expire':
            payload = {**payload, 'expires_at': time.time() + 0.08}
        return payload
    monkeypatch.setattr(ui_gateway, 'RECHECK_SECONDS', 0.01)
    with patch.object(ui_gateway, 'reauthenticate', side_effect=validate):
        with client.websocket_connect('/runtime-ui/pty/pty_42/connect', headers=headers) as socket:
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_text()
            assert exc.value.code == 1008
    assert ui_gateway.socket_slots.released.wait(2)
    assert peers[0].closed and not ui_gateway.socket_slots.counts


def test_socket_concurrency_limit_releases_after_close(socket_setup, monkeypatch):
    client, headers, peers, _ = socket_setup
    slots = TrackingSlots(total=1, per_owner=1)
    monkeypatch.setattr(ui_gateway, 'socket_slots', slots)
    path = '/runtime-ui/pty/pty_42/connect'
    with client.websocket_connect(path, headers=headers) as first:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path, headers=headers):
                pass
        assert exc.value.code == 1013
        first.send_text('still active'); assert first.receive_text() == 'still active'
    assert slots.released.wait(2)
    assert slots.counts == {}
    with client.websocket_connect(path, headers=headers) as again:
        again.send_text('new'); assert again.receive_text() == 'new'


@pytest.mark.parametrize('limit', ['bytes', 'messages'])
def test_socket_total_budgets(socket_setup, monkeypatch, limit):
    from runtime_controller import socket_relay
    if limit == 'bytes':
        monkeypatch.setattr(socket_relay, 'MAX_BYTES', 4)
    else:
        monkeypatch.setattr(socket_relay, 'MAX_MESSAGES', 1)
    client, headers, peers, _ = socket_setup
    with client.websocket_connect('/runtime-ui/pty/pty_42/connect', headers=headers) as socket:
        socket.send_text('abc'); assert socket.receive_text() == 'abc'
        socket.send_text('de')
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_text()
        assert exc.value.code == 1009
    assert peers[0].sent == ['abc']


async def test_expiry_watchdog_survives_a_stalled_permission_check():
    from runtime_controller.socket_relay import pump
    stalled = asyncio.Event()
    started = asyncio.Event()
    class Client:
        code = None
        async def receive(self):
            await stalled.wait()
        async def close(self, code):
            self.code = code
    async def guard():
        started.set()
        await stalled.wait()
    client, peer = Client(), EchoSocket()
    await asyncio.wait_for(pump(client, peer, seconds=0.05, guard=guard, interval=0.005), timeout=1)
    assert started.is_set() and client.code == 1008
