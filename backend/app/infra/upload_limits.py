"""Finite upload limits, checked before parsing and while copying bytes."""
from app.api.contracts import error_payload

import os

from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse

from app.exceptions import PayloadTooLargeError


def positive_limit(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


FILE_LIMIT = positive_limit("UPLOAD_MAX_FILE_SIZE", 256 * 1024 * 1024)
AVATAR_LIMIT = positive_limit("UPLOAD_MAX_AVATAR_SIZE", 5 * 1024 * 1024)
REQUEST_CONCURRENCY = positive_limit("UPLOAD_REQUEST_CONCURRENCY", 8)
REQUEST_LIMIT = positive_limit("UPLOAD_MAX_REQUEST_SIZE", 512 * 1024 * 1024)


def copy_bounded(source, target, max_bytes: int) -> int:
    """Read at most one byte past the limit, never write that extra byte."""
    copied = 0
    while True:
        chunk = source.read(min(1024 * 1024, max_bytes - copied + 1))
        if not chunk:
            return copied
        if copied + len(chunk) > max_bytes:
            raise PayloadTooLargeError("File too large")
        target.write(chunk)
        copied += len(chunk)


class UploadBodyLimitMiddleware:
    """Count actual ASGI bytes, including chunked bodies without Content-Length."""
    def __init__(self, app, max_bytes: int = REQUEST_LIMIT):
        self.app = app
        self.max_bytes = max_bytes
        self.active_requests = 0

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or scope.get("method") != "POST" or not path.startswith("/api/files"):
            return await self.app(scope, receive, send)
        limit = min(self.max_bytes, (AVATAR_LIMIT * 4 // 3) + 1024 * 1024) if path.startswith("/api/files/upload/avatar/") else self.max_bytes
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = -1
        if length < 0:
            return await JSONResponse(error_payload(400,"Invalid Content-Length"), status_code=400)(scope, receive, send)
        if length > limit:
            return await JSONResponse(error_payload(413,"Upload request too large"), status_code=413)(scope, receive, send)
        received = 0
        exceeded = False
        replaced = False

        async def bounded_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    # Starlette closes all partially parsed spooled files on this
                    # exception. Replace its 400 response with the intended 413.
                    raise MultiPartException("Upload request too large")
            return message

        async def bounded_send(message):
            nonlocal replaced
            if exceeded:
                if not replaced and message["type"] == "http.response.start":
                    replaced = True
                    await JSONResponse(error_payload(413,"Upload request too large"), status_code=413)(scope, receive, send)
                return
            await send(message)

        if self.active_requests >= REQUEST_CONCURRENCY:
            return await JSONResponse(error_payload(429,"Too many upload requests"), status_code=429)(scope, receive, send)
        self.active_requests += 1
        try:
            await self.app(scope, bounded_receive, bounded_send)
        finally:
            self.active_requests -= 1


class BufferedJsonBodyLimitMiddleware:
    """Bound JSON RPC before decoding; multipart uploads keep the streaming guard."""
    def __init__(self, app, max_bytes, max_concurrent=4):
        self.app = app
        self.max_bytes = max_bytes
        self.max_concurrent = max_concurrent
        self.active_requests = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = -1
        async def reject(code, message):
            await JSONResponse({'message': message, 'detail': message}, status_code=code)(scope, receive, send)
        if length < 0:
            return await reject(400, 'Invalid Content-Length')
        if length > self.max_bytes:
            return await reject(413, 'Request body too large')
        if self.active_requests >= self.max_concurrent:
            return await reject(429, 'Too many active requests')
        self.active_requests += 1
        try:
            buffered = bytearray()
            received = 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                received += len(message.get('body', b''))
                if received > self.max_bytes:
                    return await reject(413, 'Request body too large')
                buffered.extend(message.get("body", b""))
                if not message.get('more_body', False):
                    break
            body = bytes(buffered)
            del buffered
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return await receive()
            await self.app(scope, replay, send)
        finally:
            self.active_requests -= 1
