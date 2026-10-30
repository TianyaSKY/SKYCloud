"""Finite upload limits, checked before parsing and while copying bytes."""

import os
from typing import IO

from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.contracts import error_payload
from app.exceptions import PayloadTooLargeError


def positive_limit(name: str, default: int) -> int:
    """Read a positive integer limit from the environment."""
    message = f"{name} must be a positive integer"
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(message) from exc
    if value <= 0:
        raise RuntimeError(message)
    return value


FILE_LIMIT = positive_limit("UPLOAD_MAX_FILE_SIZE", 256 * 1024 * 1024)
AVATAR_LIMIT = positive_limit("UPLOAD_MAX_AVATAR_SIZE", 5 * 1024 * 1024)
REQUEST_CONCURRENCY = positive_limit("UPLOAD_REQUEST_CONCURRENCY", 8)
REQUEST_LIMIT = positive_limit("UPLOAD_MAX_REQUEST_SIZE", 512 * 1024 * 1024)


def copy_bounded(source: IO[bytes], target: IO[bytes], max_bytes: int) -> int:
    """Read at most one byte past the limit, never write that extra byte."""
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
    """Count request bytes and replace the first response once overflowed."""

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
        """Capture the ASGI channel and the per-request byte limit."""
        self._scope = scope
        self._receive = receive
        self._send = send
        self._limit = limit
        self._received = 0
        self._exceeded = False
        self._replaced = False

    async def bounded_receive(self) -> Message:
        """Forward one ASGI message, flagging any body overflow."""
        message = await self._receive()
        if message["type"] == "http.request":
            self._received += len(message.get("body", b""))
            if self._received > self._limit:
                self._exceeded = True
                # Starlette closes all partially parsed spooled files on this
                # exception. Replace its 400 response with the intended 413.
                detail = "Upload request too large"
                raise MultiPartException(detail)
        return message

    async def bounded_send(self, message: Message) -> None:
        """Send through, but emit one 413 envelope after an overflow."""
        if not self._exceeded:
            await self._send(message)
            return
        if self._replaced or message["type"] != "http.response.start":
            return
        self._replaced = True
        detail = "Upload request too large"
        await _send_error(self._scope, self._receive, self._send, 413, detail)


class UploadBodyLimitMiddleware:
    """Count actual ASGI bytes, including chunked bodies without Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int = REQUEST_LIMIT) -> None:
        """Wrap an ASGI app with the streaming upload byte limit."""
        self.app = app
        self.max_bytes = max_bytes
        self.active_requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Admit one bounded upload request at a time per concurrency slot."""
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
    """Buffer one JSON body and replay it after the admission checks."""

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
        """Capture the ASGI channel and the buffered-body byte limit."""
        self._scope = scope
        self._receive = receive
        self._send = send
        self._max_bytes = max_bytes
        self._body = b""
        self._delivered = False

    async def buffer(self) -> bool:
        """Read the whole body within the limit; False when it is rejected."""
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
        """Deliver the buffered body once, then delegate to the real receive."""
        if not self._delivered:
            self._delivered = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        return await self._receive()


class BufferedJsonBodyLimitMiddleware:
    """Bound JSON RPC before decoding; multipart uploads keep the streaming guard."""

    def __init__(
        self, app: ASGIApp, max_bytes: int, max_concurrent: int = 4,
    ) -> None:
        """Wrap an ASGI app with a buffered JSON body limit."""
        self.app = app
        self.max_bytes = max_bytes
        self.max_concurrent = max_concurrent
        self.active_requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Buffer one bounded POST body and replay it to the wrapped app."""
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
