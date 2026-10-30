"""Bounded direct WebSocket transport shared by the two runtime relay hops."""

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import HTTPException
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

transport_logger = logging.getLogger("runtime.socket.transport")
transport_logger.setLevel(logging.WARNING)
transport_logger.addHandler(logging.NullHandler())
transport_logger.propagate = False

MAX_MESSAGE: Final = 1024 * 1024
MAX_INPUT: Final = 256 * 1024
MAX_BYTES: Final = 64 * 1024 * 1024
MAX_MESSAGES: Final = 100000
MAX_SESSION_SECONDS: Final = 600
GUARD_INTERVAL_SECONDS: Final = 15
WRITE_LIMIT_BYTES: Final = 32768
MAX_QUEUE: Final = 4
CONNECT_TIMEOUT_SECONDS: Final = 5
PING_INTERVAL_SECONDS: Final = 20
PING_TIMEOUT_SECONDS: Final = 20
TERMINAL_PATH_PATTERN: Final = (
    r"(?:api/)?pty/[A-Za-z0-9_-]{1,128}/connect"
)

WS_NORMAL_CLOSURE: Final = 1000
WS_POLICY_VIOLATION: Final = 1008
WS_MESSAGE_TOO_BIG: Final = 1009
WS_INTERNAL_ERROR: Final = 1011


def terminal_path(path: str) -> bool:
    """Return whether the relay path addresses one runtime terminal."""
    return bool(re.fullmatch(TERMINAL_PATH_PATTERN, path))


class DirectConnect(connect):
    """Fixed connection policy: no redirects, no proxies, bounded frames."""

    def process_redirect(self, exc: Exception) -> Exception:
        """Refuse handshake redirects; the pinned client follows them by default."""
        # Never send either hop's credentials to a redirected endpoint.
        return exc


def direct_connect(url: str, headers: dict[str, str]) -> connect:
    """Open one bounded WebSocket connection to the runtime."""
    return DirectConnect(
        url,
        additional_headers=headers,
        proxy=None,
        logger=transport_logger,
        compression=None,
        max_size=MAX_MESSAGE,
        max_queue=MAX_QUEUE,
        write_limit=WRITE_LIMIT_BYTES,
        open_timeout=CONNECT_TIMEOUT_SECONDS,
        close_timeout=CONNECT_TIMEOUT_SECONDS,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
    )


class SocketSlots:
    """Fixed per-owner and total socket budget with a thread-safe counter."""

    def __init__(self, total: int = 64, per_owner: int = 4) -> None:
        """Set the total and per-owner relay budgets."""
        self.total, self.per_owner = total, per_owner
        self.counts: dict[str | int, int] = {}
        self.lock = threading.Lock()

    def acquire(self, owner: str | int) -> bool:
        """Reserve one relay slot for the owner, or refuse at the budget."""
        with self.lock:
            if (
                sum(self.counts.values()) >= self.total
                or self.counts.get(owner, 0) >= self.per_owner
            ):
                return False
            self.counts[owner] = self.counts.get(owner, 0) + 1
            return True

    def release(self, owner: str | int) -> None:
        """Return one relay slot held by the owner."""
        with self.lock:
            count = self.counts.get(owner, 0)
            if count <= 1:
                self.counts.pop(owner, None)
            else:
                self.counts[owner] = count - 1


class SocketLimit(Exception):
    """The relay exceeded its frame, byte or message budget."""


def _frame(message: Message) -> bytes | str:
    """Return the payload of one received ASGI WebSocket message."""
    raw = message.get("bytes")
    if raw is not None:
        return raw
    return message.get("text", "")


def _length(data: bytes | str) -> int:
    return len(data) if isinstance(data, bytes) else len(data.encode())


async def _shutdown(
    websocket: WebSocket, tasks: list["asyncio.Task[None]"], code: int,
) -> None:
    """Cancel every pump, close the client and drain the cancelled tasks."""
    for task in tasks:
        task.cancel()
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code)
    await asyncio.gather(*tasks, return_exceptions=True)


def _close_code(done: set["asyncio.Task[None]"]) -> int:
    """Map completed pump outcomes onto the fixed WebSocket close codes."""
    code = WS_NORMAL_CLOSURE
    for task in done:
        try:
            task.result()
        except SocketLimit:
            code = WS_MESSAGE_TOO_BIG
        except HTTPException:
            code = WS_POLICY_VIOLATION
        except (ConnectionClosed, WebSocketDisconnect):
            pass
        except Exception:
            # Any pump failure must still close the client with a fixed code.
            code = WS_INTERNAL_ERROR
    return code


async def _inbound(websocket: WebSocket, upstream: ClientConnection) -> None:
    """Relay client frames upstream within the input budget."""
    size = 0
    count = 0
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = _frame(message)
        length = _length(data)
        size += length
        count += 1
        if length > MAX_INPUT or size > MAX_BYTES or count > MAX_MESSAGES:
            raise SocketLimit
        await upstream.send(data)


async def _outbound(websocket: WebSocket, upstream: ClientConnection) -> None:
    """Relay runtime frames to the client within the message budget."""
    size = 0
    count = 0
    async for data in upstream:
        length = _length(data)
        size += length
        count += 1
        if length > MAX_MESSAGE or size > MAX_BYTES or count > MAX_MESSAGES:
            raise SocketLimit
        if isinstance(data, bytes):
            await websocket.send_bytes(data)
        else:
            await websocket.send_text(data)


async def _monitor(guard: Callable[[], Awaitable[None]], interval: float) -> None:
    """Re-check the runtime identity for the lifetime of the session."""
    while True:
        await asyncio.sleep(interval)
        await guard()


async def _expiry(seconds: float) -> None:
    """End the session at its fixed lifetime."""
    await asyncio.sleep(max(0, min(MAX_SESSION_SECONDS, seconds)))
    raise HTTPException(401, "Runtime UI session expired")


async def pump(
    websocket: WebSocket,
    upstream: ClientConnection,
    *,
    seconds: float,
    guard: Callable[[], Awaitable[None]],
    interval: float = GUARD_INTERVAL_SECONDS,
) -> None:
    """End both pumps on peer close, lifetime/budget exhaustion or revocation."""
    tasks = [
        asyncio.create_task(_inbound(websocket, upstream)),
        asyncio.create_task(_outbound(websocket, upstream)),
        asyncio.create_task(_monitor(guard, interval)),
        asyncio.create_task(_expiry(seconds)),
    ]
    code = WS_NORMAL_CLOSURE
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        code = _close_code(done)
    finally:
        await _shutdown(websocket, tasks, code)
