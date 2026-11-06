"""WebSocket 作用域、透明帧、配额与空闲撤销测试."""
import asyncio
from collections.abc import AsyncGenerator, Generator
import time
import threading
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.models.user import User
from app.models.workspace import Workspace
from tests.test_runtime_ui import ui, launch
from app.features.workspace import ui_gateway, ui_tickets
from runtime_common.socket_relay import SocketSlots, MAX_INPUT

_ = ui


class TrackingSlots(SocketSlots):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.released = threading.Event()

    def acquire(self, owner: str | int) -> bool:
        self.released.clear()
        return super().acquire(owner)

    def release(self, owner: str | int) -> None:
        super().release(owner)
        self.released.set()


class EchoSocket:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed: bool = False
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)
        await self.queue.put(message)

    async def __aiter__(self) -> AsyncGenerator[Any, None]:
        while True:
            yield await self.queue.get()

    async def __aenter__(self) -> "EchoSocket":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True


@pytest.fixture
def socket_setup(
    ui: Any, session: Session, test_user: User, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[TestClient, dict[str, str], list[EchoSocket], list[tuple[str, dict[str, str]]]], None, None]:
    host, raw, _ = launch(ui, test_user, test_workspace)
    headers = {'Host': host, 'Origin': 'http://' + host,
               'Cookie': ui_tickets.COOKIE + '=' + raw}
    peers: list[EchoSocket] = []
    requests: list[tuple[str, dict[str, str]]] = []

    def connect(url: str, headers: dict[str, str]) -> EchoSocket:
        peer = EchoSocket()
        peers.append(peer)
        requests.append((url, headers))
        return peer

    monkeypatch.setattr(ui_gateway, 'socket_slots', TrackingSlots())
    def _mock_reauth(raw: str, host: str, session_factory: object = None) -> object:
        return ui_tickets.authenticate(session, raw, host)

    with patch.object(ui_gateway, 'reauthenticate', side_effect=_mock_reauth), \
         patch.object(ui_gateway, 'direct_connect', side_effect=connect):
        yield ui[0], headers, peers, requests


@pytest.mark.parametrize('prefix, directory', [('pty', 'directory'), ('api/pty', 'location%5Bdirectory%5D')])
def test_socket_transparent_text_and_bytes_and_cleanup(
    socket_setup: tuple[TestClient, dict[str, str], list[EchoSocket], list[tuple[str, dict[str, str]]]],
    prefix: str,
    directory: str,
) -> None:
    client, headers, peers, requests = socket_setup
    with client.websocket_connect(f'/runtime-ui/{prefix}/pty_42/connect?{directory}=%2Fworkspace&cursor=0', headers={
        **headers, 'Authorization': 'Bearer caller', 'X-Runtime-Controller-Token': 'attacker'}) as socket:
        socket.send_text('echo hello\r')
        assert socket.receive_text() == 'echo hello\r'
        socket.send_bytes(b'\x00\xff\x04')
        assert socket.receive_bytes() == b'\x00\xff\x04'
    slots = cast(TrackingSlots, ui_gateway.socket_slots)
    assert slots.released.wait(2)
    assert peers[0].closed and not slots.counts
    url, forwarded = requests[0]
    assert '/runtimes/' + 'a' * 64 + f'/ws/{prefix}/pty_42/connect?' in url
    assert directory + '=%2Fworkspace' in url
    assert 'authorization' not in {key.lower() for key in forwarded}
    assert 'cookie' not in {key.lower() for key in forwarded}
    assert forwarded['X-Runtime-Controller-Token'] != 'attacker'


@pytest.mark.parametrize('kind', ['no-cookie', 'origin', 'host', 'path', 'query-jwt'])
def test_socket_rejects_scope_violations(
    socket_setup: tuple[TestClient, dict[str, str], list[EchoSocket], list[tuple[str, dict[str, str]]]],
    kind: str,
) -> None:
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
    slots = cast(TrackingSlots, ui_gateway.socket_slots)
    assert not peers and not slots.counts


def test_socket_rejects_oversize_before_upstream_send(
    socket_setup: tuple[TestClient, dict[str, str], list[EchoSocket], list[tuple[str, dict[str, str]]]]
) -> None:
    client, headers, peers, _ = socket_setup
    with client.websocket_connect('/runtime-ui/pty/pty_42/connect', headers=headers) as socket:
        socket.send_bytes(b'x' * (MAX_INPUT + 1))
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_bytes()
        assert exc.value.code == 1009
    slots = cast(TrackingSlots, ui_gateway.socket_slots)
    assert slots.released.wait(2)
    assert peers[0].closed and not peers[0].sent


@pytest.mark.parametrize('end', ['revoke', 'expire'])
def test_idle_socket_is_closed_on_revocation_or_expiry(
    socket_setup: tuple[TestClient, dict[str, str], list[EchoSocket], list[tuple[str, dict[str, str]]]],
    monkeypatch: pytest.MonkeyPatch,
    end: str,
) -> None:
    client, headers, peers, _ = socket_setup
    original = ui_gateway.reauthenticate
    calls = 0

    def validate(raw: str, host: str, session_factory: Any = None) -> Any:
        nonlocal calls
        calls += 1
        if end == 'revoke' and calls > 1:
            raise HTTPException(401, 'revoked')
        payload = original(raw, host, session_factory)
        if end == 'expire':
            payload = {**payload, 'expires_at': time.time() + 0.08}
        return payload

    monkeypatch.setattr(ui_gateway, 'RECHECK_SECONDS', 0.01)
    with patch.object(ui_gateway, 'reauthenticate', side_effect=validate):
        with client.websocket_connect('/runtime-ui/pty/pty_42/connect', headers=headers) as socket:
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_text()
            assert exc.value.code == 1008
    slots = cast(TrackingSlots, ui_gateway.socket_slots)
    assert slots.released.wait(2)
    assert peers[0].closed and not slots.counts


def test_socket_concurrency_limit_releases_after_close(
    socket_setup: tuple[TestClient, dict[str, str], list[EchoSocket], list[tuple[str, dict[str, str]]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, headers, _, _ = socket_setup
    slots = TrackingSlots(total=1, per_owner=1)
    monkeypatch.setattr(ui_gateway, 'socket_slots', slots)
    path = '/runtime-ui/pty/pty_42/connect'
    with client.websocket_connect(path, headers=headers) as first:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path, headers=headers):
                pass
        assert exc.value.code == 1013
        first.send_text('still active')
        assert first.receive_text() == 'still active'
    assert slots.released.wait(2)
    assert slots.counts == {}
    with client.websocket_connect(path, headers=headers) as again:
        again.send_text('new')
        assert again.receive_text() == 'new'


def test_terminal_bridge_failure_closes_1011_and_releases_owner_for_next_connection(
    socket_setup: tuple[TestClient, dict[str, str], list[EchoSocket], list[tuple[str, dict[str, str]]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, headers, _, _ = socket_setup
    slots = TrackingSlots(total=2, per_owner=1)
    monkeypatch.setattr(ui_gateway, 'socket_slots', slots)
    connect = ui_gateway.direct_connect
    fail_handshake = True

    class BrokenHandshake(EchoSocket):
        async def __aenter__(self) -> "BrokenHandshake":
            raise RuntimeError('private upstream handshake failure')

    def upstream(url: str, headers: dict[str, str]) -> Any:
        nonlocal fail_handshake
        if fail_handshake:
            fail_handshake = False
            return BrokenHandshake()
        return connect(url, headers)

    monkeypatch.setattr(ui_gateway, 'direct_connect', upstream)
    path = '/runtime-ui/pty/pty_42/connect'
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, headers=headers):
            pass
    assert exc.value.code == 1011
    assert exc.value.reason == ''
    assert slots.released.wait(2)

    with client.websocket_connect(path, headers=headers) as following:
        following.send_text('owner slot reused')
        assert following.receive_text() == 'owner slot reused'
    assert slots.released.wait(2)


@pytest.mark.parametrize('limit', ['bytes', 'messages'])
def test_socket_total_budgets(
    socket_setup: tuple[TestClient, dict[str, str], list[EchoSocket], list[tuple[str, dict[str, str]]]],
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    import runtime_common.socket_relay as socket_relay
    if limit == 'bytes':
        monkeypatch.setattr(socket_relay, 'MAX_BYTES', 4)
    else:
        monkeypatch.setattr(socket_relay, 'MAX_MESSAGES', 1)
    client, headers, peers, _ = socket_setup
    with client.websocket_connect('/runtime-ui/pty/pty_42/connect', headers=headers) as socket:
        socket.send_text('abc')
        assert socket.receive_text() == 'abc'
        socket.send_text('de')
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_text()
        assert exc.value.code == 1009
    assert peers[0].sent == ['abc']


async def test_expiry_watchdog_survives_a_stalled_permission_check() -> None:
    from runtime_common.socket_relay import pump
    stalled = asyncio.Event()
    started = asyncio.Event()

    class Client:
        code: int | None = None

        async def receive(self) -> None:
            await stalled.wait()

        async def close(self, code: int) -> None:
            self.code = code

    async def guard() -> None:
        started.set()
        await stalled.wait()

    client, peer = Client(), EchoSocket()
    await asyncio.wait_for(
        pump(cast(Any, client), cast(Any, peer), seconds=0.05, guard=guard, interval=0.005),
        timeout=1,
    )
    assert started.is_set() and client.code == 1008
