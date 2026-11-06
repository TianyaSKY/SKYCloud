"""有界的上传大小限制, 在解析前和复制字节流时执行校验."""

from typing import IO

from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.contracts import error_payload
from app.config.upload import get_upload_limits_config
from app.config.upload import positive_limit as positive_limit  # noqa: PLC0414
from app.exceptions import PayloadTooLargeError

_upload_limits_cfg = get_upload_limits_config()
FILE_LIMIT = _upload_limits_cfg.max_file_size
AVATAR_LIMIT = _upload_limits_cfg.max_avatar_size
REQUEST_CONCURRENCY = _upload_limits_cfg.request_concurrency
REQUEST_LIMIT = _upload_limits_cfg.max_request_size


def copy_bounded(source: IO[bytes], target: IO[bytes], max_bytes: int) -> int:
    """最多读取超出限制一个字节的内容, 绝不写入该额外字节."""
    copied = 0
    while True:
        chunk = source.read(min(1024 * 1024, max_bytes - copied + 1))
        if not chunk:
            return copied
        if copied + len(chunk) > max_bytes:
            message = "File too large"
            raise PayloadTooLargeError(message)
        target.write(chunk)
        copied += len(chunk)


def _content_length(scope: Scope) -> int:
    headers: dict[bytes, bytes] = dict(scope.get("headers", []))
    try:
        return int(headers.get(b"content-length", b"0"))
    except ValueError:
        return -1


async def _send_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    status: int,
    message: str,
) -> None:
    response = JSONResponse(error_payload(status, message), status_code=status)
    await response(scope, receive, send)


async def _send_json_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    status: int,
    message: str,
) -> None:
    payload = {"message": message, "detail": message}
    response = JSONResponse(payload, status_code=status)
    await response(scope, receive, send)


class _UploadBodyGuard:
    """统计请求字节数, 一旦溢出则替换首个响应."""

    __slots__ = (
        "_exceeded",
        "_limit",
        "_receive",
        "_received",
        "_replaced",
        "_scope",
        "_send",
    )

    def __init__(
        self, scope: Scope, receive: Receive, send: Send, limit: int,
    ) -> None:
        """捕获 ASGI 通道及单个请求的字节配额限制."""
        self._scope = scope
        self._receive = receive
        self._send = send
        self._limit = limit
        self._received = 0
        self._exceeded = False
        self._replaced = False

    async def bounded_receive(self) -> Message:
        """转发单条 ASGI 消息, 标记任何请求体溢出状态."""
        message = await self._receive()
        if message["type"] == "http.request":
            self._received += len(message.get("body", b""))
            if self._received > self._limit:
                self._exceeded = True
                # Starlette 在此异常发生时会关闭所有部分解析的缓冲文件。
                # 将其默认的 400 响应替换为预期的 413 响应。
                detail = "Upload request too large"
                raise MultiPartException(detail)
        return message

    async def bounded_send(self, message: Message) -> None:
        """正常发送消息, 但在溢出后发送一次 413 响应外包."""
        if not self._exceeded:
            await self._send(message)
            return
        if self._replaced or message["type"] != "http.response.start":
            return
        self._replaced = True
        detail = "Upload request too large"
        await _send_error(self._scope, self._receive, self._send, 413, detail)


class UploadBodyLimitMiddleware:
    """统计实际 ASGI 字节数, 包括不带 Content-Length 的分块请求体."""

    def __init__(self, app: ASGIApp, max_bytes: int = REQUEST_LIMIT) -> None:
        """为 ASGI 应用包装流式上传字节配额限制."""
        self.app = app
        self.max_bytes = max_bytes
        self.active_requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """按并发槽位每次准入一个有界上传请求."""
        if not _is_upload_request(scope):
            await self.app(scope, receive, send)
            return
        limit = self._upload_limit(str(scope.get("path", "")))
        length = _content_length(scope)
        if length < 0:
            await _send_error(scope, receive, send, 400, "Invalid Content-Length")
            return
        if length > limit:
            await _send_error(scope, receive, send, 413, "Upload request too large")
            return
        if self.active_requests >= REQUEST_CONCURRENCY:
            await _send_error(scope, receive, send, 429, "Too many upload requests")
            return
        guard = _UploadBodyGuard(scope, receive, send, limit)
        self.active_requests += 1
        try:
            await self.app(scope, guard.bounded_receive, guard.bounded_send)
        finally:
            self.active_requests -= 1

    def _upload_limit(self, path: str) -> int:
        if path.startswith("/api/files/upload/avatar/"):
            return min(self.max_bytes, (AVATAR_LIMIT * 4 // 3) + 1024 * 1024)
        return self.max_bytes


def _is_upload_request(scope: Scope) -> bool:
    path = str(scope.get("path", ""))
    return (
        scope["type"] == "http"
        and scope.get("method") == "POST"
        and path.startswith("/api/files")
    )


class _BufferedJsonGuard:
    """缓冲单个 JSON 请求体, 并在准入检查通过后进行回放."""

    __slots__ = (
        "_body",
        "_delivered",
        "_max_bytes",
        "_receive",
        "_scope",
        "_send",
    )

    def __init__(
        self, scope: Scope, receive: Receive, send: Send, max_bytes: int,
    ) -> None:
        """捕获 ASGI 通道及缓冲请求体的字节配额限制."""
        self._scope = scope
        self._receive = receive
        self._send = send
        self._max_bytes = max_bytes
        self._body = b""
        self._delivered = False

    async def buffer(self) -> bool:
        """在限制范围内读取全部请求体; 被拒绝时返回 False."""
        chunks = bytearray()
        received = 0
        while True:
            message = await self._receive()
            if message["type"] == "http.disconnect":
                return False
            body = message.get("body", b"")
            received += len(body)
            if received > self._max_bytes:
                await _send_json_error(
                    self._scope,
                    self._receive,
                    self._send,
                    413,
                    "Request body too large",
                )
                return False
            chunks.extend(body)
            if not message.get("more_body", False):
                break
        self._body = bytes(chunks)
        return True

    async def replay(self) -> Message:
        """投递一次缓冲的请求体, 随后委托给真实的 receive 函数."""
        if not self._delivered:
            self._delivered = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        return await self._receive()


class BufferedJsonBodyLimitMiddleware:
    """在解码前限制 JSON RPC 大小; 多部分上传保持使用流式防护."""

    def __init__(
        self, app: ASGIApp, max_bytes: int, max_concurrent: int = 4,
    ) -> None:
        """为 ASGI 应用包装缓冲 JSON 请求体限制."""
        self.app = app
        self.max_bytes = max_bytes
        self.max_concurrent = max_concurrent
        self.active_requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """缓冲单个有界 POST 请求体并回放给被包装的应用."""
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        length = _content_length(scope)
        if length < 0:
            await _send_json_error(scope, receive, send, 400, "Invalid Content-Length")
            return
        if length > self.max_bytes:
            await _send_json_error(
                scope, receive, send, 413, "Request body too large",
            )
            return
        if self.active_requests >= self.max_concurrent:
            await _send_json_error(
                scope, receive, send, 429, "Too many active requests",
            )
            return
        guard = _BufferedJsonGuard(scope, receive, send, self.max_bytes)
        self.active_requests += 1
        try:
            if await guard.buffer():
                await self.app(scope, guard.replay, send)
        finally:
            self.active_requests -= 1
