"""两条 Runtime 中继跃点之间共享的有界直接 WebSocket 传输层."""

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
    """判断中继路径是否对应某个运行时终端."""
    return bool(re.fullmatch(TERMINAL_PATH_PATTERN, path))


class DirectConnect(connect):
    """固定连接策略: 无重定向、无代理、有界帧限制."""

    def process_redirect(self, exc: Exception) -> Exception:
        """拒绝握手重定向; 固定的客户端默认会跟随重定向."""
        # 严禁将任一跃点的凭证发送至被重定向的端点。
        return exc


def direct_connect(url: str, headers: dict[str, str]) -> connect:
    """打开一个直连到运行时的有界 WebSocket 连接."""
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
    """具备线程安全计数器的固定单所有者和全局 Socket 配额管理."""

    def __init__(self, total: int = 64, per_owner: int = 4) -> None:
        """设置全局及单所有者中继配额上限."""
        self.total, self.per_owner = total, per_owner
        self.counts: dict[str | int, int] = {}
        self.lock = threading.Lock()

    def acquire(self, owner: str | int) -> bool:
        """为所有者预留一个中继槽位, 超过配额则拒绝."""
        with self.lock:
            if (
                sum(self.counts.values()) >= self.total
                or self.counts.get(owner, 0) >= self.per_owner
            ):
                return False
            self.counts[owner] = self.counts.get(owner, 0) + 1
            return True

    def release(self, owner: str | int) -> None:
        """归还所有者持有的一个中继槽位."""
        with self.lock:
            count = self.counts.get(owner, 0)
            if count <= 1:
                self.counts.pop(owner, None)
            else:
                self.counts[owner] = count - 1


class SocketLimit(Exception):
    """中继超出帧大小、字节数或消息计数配额异常."""


def _frame(message: Message) -> bytes | str:
    """获取接收到的 ASGI WebSocket 消息负载."""
    raw = message.get("bytes")
    if raw is not None:
        return raw
    return message.get("text", "")


def _length(data: bytes | str) -> int:
    return len(data) if isinstance(data, bytes) else len(data.encode())


async def _shutdown(
    websocket: WebSocket, tasks: list["asyncio.Task[None]"], code: int,
) -> None:
    """取消所有泵任务, 关闭客户端连接并排空已取消的任务."""
    for task in tasks:
        task.cancel()
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code)
    await asyncio.gather(*tasks, return_exceptions=True)


def _close_code(done: set["asyncio.Task[None]"]) -> int:
    """将已完成的泵任务结果映射为固定的 WebSocket 关闭状态码."""
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
            # 任何泵任务失败均须使用固定的状态码关闭客户端。
            code = WS_INTERNAL_ERROR
    return code


async def _inbound(websocket: WebSocket, upstream: ClientConnection) -> None:
    """在输入配额限制内将客户端帧中继发送至上游."""
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
    """在消息配额限制内将运行时帧中继发送至客户端."""
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
    """在会话生命周期内持续复核运行时身份有效性."""
    while True:
        await asyncio.sleep(interval)
        await guard()


async def _expiry(seconds: float) -> None:
    """在到达固定生存时间时终止会话."""
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
    """在对端关闭、超时/配额耗尽或权限吊销时终止双向数据泵."""
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
