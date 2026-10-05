"""Bounded direct WebSocket transport shared by the two runtime relay hops."""
import asyncio
import re
import logging
import threading

from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

transport_logger = logging.Logger('runtime.socket.transport', level=logging.WARNING)
transport_logger.addHandler(logging.NullHandler())
transport_logger.propagate = False

MAX_MESSAGE = 1024 * 1024
MAX_INPUT = 256 * 1024
MAX_BYTES = 64 * 1024 * 1024
MAX_MESSAGES = 100000


def terminal_path(path):
    return bool(re.fullmatch(r'(?:api/)?pty/[A-Za-z0-9_-]{1,128}/connect', path))


class DirectConnect(connect):
    # websockets 15.0.1 follows handshake redirects by default. Never send
    # either hop's credentials to a redirected endpoint; the version is pinned.
    def process_redirect(self, exc):
        return exc


def direct_connect(url, headers):
    return DirectConnect(url, additional_headers=headers, proxy=None,
        logger=transport_logger, compression=None, max_size=MAX_MESSAGE, max_queue=4, write_limit=32768,
        open_timeout=5, close_timeout=5, ping_interval=20, ping_timeout=20)


class SocketSlots:
    def __init__(self, total=64, per_owner=4):
        self.total, self.per_owner = total, per_owner
        self.counts = {}
        self.lock = threading.Lock()

    def acquire(self, owner):
        with self.lock:
            if sum(self.counts.values()) >= self.total or self.counts.get(owner, 0) >= self.per_owner:
                return False
            self.counts[owner] = self.counts.get(owner, 0) + 1
            return True

    def release(self, owner):
        with self.lock:
            count = self.counts.get(owner, 0)
            if count <= 1:
                self.counts.pop(owner, None)
            else:
                self.counts[owner] = count - 1


class SocketLimit(Exception):
    pass


async def pump(websocket, upstream, *, seconds, guard, interval=15):
    """End both pumps on peer close, lifetime/budget exhaustion or revocation."""
    async def inbound():
        size = count = 0
        while True:
            message = await websocket.receive()
            if message['type'] == 'websocket.disconnect':
                return
            data = message.get('bytes') if message.get('bytes') is not None else message.get('text', '')
            length = len(data) if isinstance(data, bytes) else len(data.encode())
            size += length; count += 1
            if length > MAX_INPUT or size > MAX_BYTES or count > MAX_MESSAGES:
                raise SocketLimit()
            await upstream.send(data)

    async def outbound():
        size = count = 0
        async for data in upstream:
            length = len(data) if isinstance(data, bytes) else len(data.encode())
            size += length; count += 1
            if length > MAX_MESSAGE or size > MAX_BYTES or count > MAX_MESSAGES:
                raise SocketLimit()
            if isinstance(data, bytes):
                await websocket.send_bytes(data)
            else:
                await websocket.send_text(data)

    async def monitor():
        while True:
            await asyncio.sleep(interval)
            await guard()

    async def expiry():
        await asyncio.sleep(max(0, min(600, seconds)))
        raise HTTPException(401, 'Runtime UI session expired')

    tasks = [asyncio.create_task(fn()) for fn in (inbound, outbound, monitor, expiry)]
    code = 1000
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                task.result()
            except SocketLimit:
                code = 1009
            except HTTPException:
                code = 1008
            except (ConnectionClosed, WebSocketDisconnect):
                pass
            except Exception:
                code = 1011
    finally:
        for task in tasks:
            task.cancel()
        try:
            await websocket.close(code=code)
        except (RuntimeError, WebSocketDisconnect):
            pass
        await asyncio.gather(*tasks, return_exceptions=True)
