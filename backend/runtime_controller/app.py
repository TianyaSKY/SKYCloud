"""Authenticated, bounded RPC and HTTP relay; no general Docker or exec endpoint."""

import asyncio
import base64
import contextlib
import hmac
import logging
import os
import socket
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any, Final, Literal, cast
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from runtime_common.contracts import ContainerPayload, UserSnapshotRow
from runtime_common.http_decode import (
    ACCEPT_ENCODING,
    content_encoding,
    decoded_bytes,
)
from runtime_common.http_stream import (
    UpstreamLease,
    UpstreamResponse,
    deadline_reader,
)
from runtime_common.http_wait import WaitPolicy, guarded_wait
from runtime_common.json_types import JsonObject
from runtime_common.socket_relay import (
    MAX_MESSAGE,
    SocketSlots,
    direct_connect,
    pump,
    terminal_path,
)
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from runtime_controller.engine import (
    Engine,
    MissingGenerationCandidate,
    MissingRuntime,
    PolicyViolation,
)
from runtime_controller.policy import Settings

logger = logging.getLogger(__name__)

MAX_BODY: Final = 256 * 1024
MAX_BODY_SECONDS: Final = 30
MAX_BODY_IDLE_SECONDS: Final = 10
MAX_RESPONSE: Final = 64 * 1024 * 1024
MAX_HEADER_SECONDS: Final = 90
MAX_HEALTH_HEADER_SECONDS: Final = 2
MAX_RESPONSE_SECONDS: Final = 120
MAX_EVENT_SECONDS: Final = 3600
MAX_QUERY_BYTES: Final = 4096
TERMINAL_SECONDS: Final = 600
RECONCILE_INTERVAL_SECONDS: Final = 1
HEALTH_TIMEOUT_SECONDS: Final = 2
HEALTH_CONNECT_TIMEOUT_SECONDS: Final = 1
RELAY_TIMEOUT_SECONDS: Final = 90
RELAY_CONNECT_TIMEOUT_SECONDS: Final = 5
DEFAULT_MEDIA_TYPE: Final = "application/json"
EVENT_MEDIA_TYPE: Final = "text/event-stream"
RELAY_ROOTS: Final = frozenset(
    {
        "global",
        "event",
        "session",
        "permission",
        "question",
        "mcp",
        "config",
        "instance",
        "agent",
        "project",
        "path",
        "vcs",
        "file",
        "find",
        "experimental",
        "command",
        "log",
        "provider",
        "lsp",
        "formatter",
        "doc",
    },
)
TERMINAL_QUERY_KEYS: Final = frozenset(
    {"directory", "location[directory]", "workspace", "cursor", "ticket"},
)
FORWARDED_HEADERS: Final = ("content-type", "accept")

STATUS_UNAUTHORIZED: Final = 401
STATUS_FORBIDDEN: Final = 403
STATUS_REQUEST_TIMEOUT: Final = 408
STATUS_TOO_LARGE: Final = 413
STATUS_UNPROCESSABLE: Final = 422
STATUS_CONFLICT: Final = 409
STATUS_CLIENT_CLOSED: Final = 499
STATUS_SERVER_ERROR: Final = 500
STATUS_BAD_GATEWAY: Final = 502
STATUS_GATEWAY_TIMEOUT: Final = 504

WS_POLICY_VIOLATION: Final = 1008
WS_INTERNAL_ERROR: Final = 1011
WS_TRY_AGAIN_LATER: Final = 1013


class PrepareRequest(BaseModel):
    """Fixed runtime identity plus the generation to prepare."""

    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0, le=2**63 - 1)
    user_id: int = Field(gt=0, le=2**63 - 1)
    runtime_id: int = Field(gt=0, le=2**63 - 1)
    generation: str = Field(pattern=r"^[0-9a-f]{32}$")


async def _idle_check() -> None:
    """Return without further checks; the bounded wait owns progress."""


def _raise_request_timeout() -> None:
    raise HTTPException(STATUS_REQUEST_TIMEOUT, "Controller request timed out")


def _request_headers(scope: Scope) -> dict[bytes, bytes]:
    """Return the lowercased ASGI request headers."""
    headers: dict[bytes, bytes] = {}
    for key, value in scope.get("headers", []):
        headers[key.lower()] = value
    return headers


def _replay(receive: Receive, body: bytes) -> Callable[[], Awaitable[Message]]:
    consumed = False

    async def replay() -> Message:
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


class AuthAndBodyLimit:
    """Authenticate every HTTP request and bound its body read."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        """Wrap the Controller application with its fixed credential."""
        self.app, self.token = app, token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate, bound and replay one ASGI request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not hmac.compare_digest(
            _request_headers(scope).get(b"x-runtime-controller-token", b""),
            self.token,
        ):
            await JSONResponse(
                {"detail": "Controller authentication required"},
                status_code=STATUS_UNAUTHORIZED,
            )(scope, receive, send)
            return
        body = await self._read_body(receive, scope, send)
        if body is None:
            return
        await self.app(scope, _replay(receive, body), send)

    async def _read_body(
        self, receive: Receive, scope: Scope, send: Send,
    ) -> bytes | None:
        """Read the bounded body, or answer with the fixed error response."""
        body = bytearray()
        deadline = monotonic() + MAX_BODY_SECONDS
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    _raise_request_timeout()
                message = await guarded_wait(
                    receive(),
                    WaitPolicy(
                        check=_idle_check,
                        seconds=min(MAX_BODY_IDLE_SECONDS, remaining),
                        status=STATUS_REQUEST_TIMEOUT,
                        detail="Controller request timed out",
                    ),
                )
                if monotonic() >= deadline:
                    _raise_request_timeout()
                if message["type"] == "http.disconnect":
                    return None
                if len(body) + len(message.get("body", b"")) > MAX_BODY:
                    await JSONResponse(
                        {"detail": "Controller request too large"},
                        status_code=STATUS_TOO_LARGE,
                    )(scope, receive, send)
                    return None
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    return bytes(body)
        except HTTPException as exc:
            if exc.status_code != STATUS_REQUEST_TIMEOUT:
                raise
            await JSONResponse(
                {"detail": "Controller request timed out"},
                status_code=STATUS_REQUEST_TIMEOUT,
            )(scope, receive, send)
            return None


def _lifespan(
    engine: Engine,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        stopping = asyncio.Event()

        async def reconcile() -> None:
            while not stopping.is_set():
                try:
                    await run_in_threadpool(engine.reconcile_retired)
                except Exception as error:
                    # One failed Docker scan must never stop the retired sweep.
                    logger.warning(
                        "Runtime retirement scan deferred (%s)", type(error).__name__,
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stopping.wait(), timeout=RECONCILE_INTERVAL_SECONDS,
                    )

        task = asyncio.create_task(reconcile())
        try:
            yield
        finally:
            stopping.set()
            await task  # Drain bounded Docker RPCs before releasing the engine.

    return lifespan


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation(
        _request: Request, _exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Invalid controller request"}, status_code=STATUS_UNPROCESSABLE,
        )

    @app.exception_handler(MissingRuntime)
    async def missing(_request: Request, _exc: MissingRuntime) -> JSONResponse:
        return JSONResponse({"detail": "Runtime not found"}, status_code=404)

    @app.exception_handler(MissingGenerationCandidate)
    async def missing_generation(
        _request: Request, exc: MissingGenerationCandidate,
    ) -> JSONResponse:
        return JSONResponse(
            {
                "code": "generation_candidate_missing",
                "generation": exc.generation,
                "container_id": exc.container_id,
                "detail": "Recorded generation candidate is missing",
            },
            status_code=STATUS_CONFLICT,
        )

    @app.exception_handler(PolicyViolation)
    async def forbidden(_request: Request, _exc: PolicyViolation) -> JSONResponse:
        return JSONResponse(
            {"detail": "Runtime operation forbidden"}, status_code=STATUS_FORBIDDEN,
        )

    @app.exception_handler(Exception)
    async def error(_request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Runtime controller operation failed (error_type=%s)", type(exc).__name__,
        )
        return JSONResponse(
            {"detail": "Runtime controller operation failed"},
            status_code=STATUS_SERVER_ERROR,
        )


def _validate_user_id(user_id: int) -> None:
    if user_id <= 0 or user_id > 2**63 - 1:
        raise HTTPException(STATUS_UNPROCESSABLE, "Invalid user identity")


def _register_health_routes(app: FastAPI) -> None:
    @app.get("/health", response_model=None)
    def health() -> dict[str, str]:
        return {"status": "ok"}

def _register_container_routes(app: FastAPI, engine: Engine) -> None:

    @app.post("/containers/prepare", response_model=None)
    def prepare(payload: PrepareRequest) -> ContainerPayload:
        return engine.prepare(**payload.model_dump())

    @app.post("/containers/retire-generation", response_model=None)
    def retire_generation(payload: PrepareRequest) -> dict[str, bool]:
        engine.retire_generation(**payload.model_dump())
        return {"success": True}

    @app.get("/users/{user_id}/containers", response_model=None)
    def user_snapshot(user_id: int) -> list[UserSnapshotRow]:
        _validate_user_id(user_id)
        return engine.user_snapshot(user_id)

    @app.get("/containers/{ref}", response_model=None)
    def inspect(ref: str) -> ContainerPayload:
        return engine.inspect(ref)

    @app.post("/containers/{ref}/actions/{action}", response_model=None)
    def action(
        ref: str, action: Literal["start", "stop", "restart", "remove"],
    ) -> dict[str, bool]:
        engine.action(ref, action)
        return {"success": True}

    @app.post("/retired-containers/{ref}/remove", response_model=None)
    def remove_legacy(ref: str) -> dict[str, bool]:
        engine.remove_legacy(ref)
        return {"success": True}


def _register_config_routes(app: FastAPI, engine: Engine) -> None:
    @app.get("/containers/{ref}/config", response_model=None)
    def read_config(ref: str) -> JsonObject:
        return engine.read_config(ref)

    @app.put("/containers/{ref}/config", response_model=None)
    def write_config(ref: str, payload: dict[str, Any]) -> dict[str, bool]:
        # The request body is already parsed JSON; the fixed file writer owns
        # its shape and never interprets caller input as a path or command.
        engine.write_config(ref, cast("JsonObject", payload))
        return {"success": True}


@dataclass(frozen=True, slots=True)
class RelayTarget:
    """One validated relay request and its runtime reference."""

    request: Request
    ref: str
    path: str
    is_ui: bool


def _relay_path_allowed(path: str, *, is_ui: bool) -> bool:
    """Return whether the relay path stays inside the fixed endpoint set."""
    if path.startswith("/") or "\\" in path or ":" in path:
        return False
    if any(part in {".", ".."} for part in path.split("/")):
        return False
    if is_ui:
        return True
    return bool(path) and path.split("/", 1)[0] in RELAY_ROOTS


def _disconnect_check(request: Request) -> Callable[[], Awaitable[None]]:
    async def check() -> None:
        # Body is already consumed by admission middleware and Request.
        if await request.is_disconnected():
            raise HTTPException(STATUS_CLIENT_CLOSED, "Runtime client disconnected")

    return check


def _upstream_timeout(path: str) -> httpx.Timeout:
    # Cold-start health requests must return promptly; leaving failed
    # probes open for 90 seconds interferes with readiness retries.
    if path == "global/health":
        return httpx.Timeout(
            HEALTH_TIMEOUT_SECONDS, connect=HEALTH_CONNECT_TIMEOUT_SECONDS,
        )
    return httpx.Timeout(RELAY_TIMEOUT_SECONDS, connect=RELAY_CONNECT_TIMEOUT_SECONDS)


async def _open_upstream(
    client: httpx.AsyncClient,
    lease: UpstreamLease,
    relay: RelayTarget,
    target: str,
    check: Callable[[], Awaitable[None]],
) -> httpx.Response:
    """Send the relayed request through the bounded admission wait."""
    request = relay.request
    headers = {
        key: request.headers[key] for key in FORWARDED_HEADERS if key in request.headers
    }
    headers["Accept-Encoding"] = ACCEPT_ENCODING
    if relay.is_ui and request.headers.get("x-opencode-ticket") == "1":
        headers["x-opencode-ticket"] = "1"
    upstream = client.build_request(
        request.method,
        target + "/" + relay.path,
        params=list(request.query_params.multi_items()),
        content=await request.body(),
        headers=headers,
    )

    async def open_response() -> httpx.Response:
        opened = await client.send(upstream, stream=True)
        lease.upstream = opened
        return opened

    return await guarded_wait(
        open_response(),
        WaitPolicy(
            check=check,
            seconds=(
                MAX_HEALTH_HEADER_SECONDS
                if relay.path == "global/health"
                else MAX_HEADER_SECONDS
            ),
            status=STATUS_GATEWAY_TIMEOUT,
            detail="Runtime upstream timed out",
            on_abort=lease.close,
        ),
    )


def _extend_output(output: bytearray, chunk: bytes) -> None:
    if len(output) + len(chunk) > MAX_RESPONSE:
        raise HTTPException(STATUS_BAD_GATEWAY, "Runtime response limit exceeded")
    output.extend(chunk)


async def _buffered_response(
    response: httpx.Response,
    lease: UpstreamLease,
    check: Callable[[], Awaitable[None]],
) -> Response:
    """Collect one bounded non-event body and release the lease."""
    try:
        output = bytearray()
        parts = deadline_reader(
            decoded_bytes(response, MAX_RESPONSE),
            MAX_RESPONSE_SECONDS,
            check=check,
        )
        async with aclosing(parts) as chunks:
            async for chunk in chunks:
                _extend_output(output, chunk)
        return Response(
            bytes(output),
            status_code=response.status_code,
            headers={
                "Content-Type": response.headers.get(
                    "content-type", DEFAULT_MEDIA_TYPE,
                ),
            },
        )
    except httpx.HTTPError as exc:
        raise HTTPException(STATUS_BAD_GATEWAY, "Runtime unavailable") from exc
    finally:
        await lease.close()


def _event_response(
    response: httpx.Response,
    lease: UpstreamLease,
    check: Callable[[], Awaitable[None]],
) -> UpstreamResponse:
    """Stream one bounded event body and release the lease on teardown."""

    async def events() -> AsyncIterator[bytes]:
        total = 0
        try:
            parts = deadline_reader(
                decoded_bytes(response, MAX_RESPONSE),
                MAX_EVENT_SECONDS,
                check=check,
            )
            async with aclosing(parts) as chunks:
                async for chunk in chunks:
                    total += len(chunk)
                    if total > MAX_RESPONSE:
                        break
                    yield chunk
        except (httpx.HTTPError, HTTPException):
            return
        finally:
            await lease.close()

    return UpstreamResponse(
        events(),
        lease=lease,
        status_code=response.status_code,
        media_type=EVENT_MEDIA_TYPE,
        headers={"Cache-Control": "no-store"},
    )


async def _relay_response(relay: RelayTarget, engine: Engine) -> Response:
    """Relay one bounded request to its managed runtime."""
    target, auth = await run_in_threadpool(engine.target, relay.ref)
    client = httpx.AsyncClient(
        auth=auth,
        timeout=_upstream_timeout(relay.path),
        trust_env=False,
        follow_redirects=False,
    )
    lease = UpstreamLease(client)
    check = _disconnect_check(relay.request)
    try:
        response = await _open_upstream(client, lease, relay, target, check)
        content_encoding(response)
    except httpx.HTTPError as exc:
        logger.warning("Runtime relay transport failed: %s", type(exc).__name__)
        await lease.close()
        raise HTTPException(STATUS_BAD_GATEWAY, "Runtime unavailable") from exc
    except BaseException:
        await lease.close()
        raise
    if EVENT_MEDIA_TYPE in response.headers.get("content-type", ""):
        return _event_response(response, lease, check)
    return await _buffered_response(response, lease, check)


def _terminal_allowed(websocket: WebSocket, token: str, path: str) -> bool:
    """Return whether the WebSocket carries a valid, bounded terminal request."""
    provided = websocket.headers.get("x-runtime-controller-token", "").encode()
    if not hmac.compare_digest(provided, token.encode()):
        return False
    if (
        not terminal_path(path)
        or len(websocket.scope.get("query_string", b"")) > MAX_QUERY_BYTES
    ):
        return False
    return all(key in TERMINAL_QUERY_KEYS for key in websocket.query_params)


def _terminal_url(target: str, path: str, websocket: WebSocket) -> str:
    url = target.replace("http://", "ws://", 1) + "/" + path
    query = urlencode(list(websocket.query_params.multi_items()))
    return url + "?" + query if query else url


def _identity_guard(
    engine: Engine,
    ref: str,
    target: str,
    auth: tuple[str, str],
) -> Callable[[], Awaitable[None]]:
    async def guard() -> None:
        current = await run_in_threadpool(engine.target, ref)
        if current != (target, auth):
            raise HTTPException(STATUS_UNAUTHORIZED, "Runtime identity changed")

    return guard


async def _run_terminal(
    websocket: WebSocket,
    ref: str,
    path: str,
    engine: Engine,
    slots: SocketSlots,
) -> None:
    """Open one bounded terminal relay and always release its slot."""
    acquired = False
    try:
        target, auth = await run_in_threadpool(engine.target, ref)
        if not slots.acquire(ref):
            await websocket.close(code=WS_TRY_AGAIN_LATER)
            return
        acquired = True
        url = _terminal_url(target, path, websocket)
        credential = base64.b64encode((auth[0] + ":" + auth[1]).encode()).decode()
        async with direct_connect(
            url, {"Authorization": "Basic " + credential},
        ) as upstream:
            await websocket.accept()
            await pump(
                websocket,
                upstream,
                seconds=TERMINAL_SECONDS,
                guard=_identity_guard(engine, ref, target, auth),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        # A failed relay must close the client with the fixed error code.
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=WS_INTERNAL_ERROR)
    finally:
        if acquired:
            slots.release(ref)


def _register_relay_routes(
    app: FastAPI, settings: Settings, engine: Engine, slots: SocketSlots,
) -> None:
    @app.websocket("/runtimes/{ref}/ws/{path:path}")
    async def terminal(ref: str, path: str, websocket: WebSocket) -> None:
        if not _terminal_allowed(websocket, settings.token, path):
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        await _run_terminal(websocket, ref, path, engine, slots)

    @app.api_route(
        "/runtimes/{ref}/ui/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        response_model=None,
    )
    @app.api_route(
        "/runtimes/{ref}/http/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        response_model=None,
    )
    async def relay(ref: str, path: str, request: Request) -> Response:
        is_ui = request.url.path.startswith("/runtimes/" + ref + "/ui/")
        if not _relay_path_allowed(path, is_ui=is_ui):
            raise HTTPException(STATUS_FORBIDDEN, "Unsupported runtime HTTP path")
        return await _relay_response(RelayTarget(request, ref, path, is_ui), engine)


def create_app(
    settings: Settings | None = None, engine: Engine | None = None,
) -> FastAPI:
    """Build the authenticated, bounded Controller application."""
    active_settings = settings or Settings.from_env()
    active_engine = engine or Engine(active_settings)
    slots = SocketSlots()
    app = FastAPI(
        title="SKYCloud Runtime Controller",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan(active_engine),
    )
    app.add_middleware(AuthAndBodyLimit, token=active_settings.token)
    _register_exception_handlers(app)
    _register_health_routes(app)
    _register_container_routes(app, active_engine)
    _register_config_routes(app, active_engine)
    _register_relay_routes(app, active_settings, active_engine, slots)
    return app


if __name__ == "__main__":
    import uvicorn

    from runtime_controller.diagnostics import setup_logging

    setup_logging()

    # Compose binds only the private control interface. Runtime containers
    # cannot contact the RPC listener on its separate runtime interface.
    bind_host = os.getenv("RUNTIME_CONTROLLER_BIND_HOST", "127.0.0.1")
    if bind_host != "127.0.0.1":
        bind_host = socket.gethostbyname(bind_host)
    uvicorn.run(
        create_app(),
        host=bind_host,
        port=int(os.getenv("RUNTIME_CONTROLLER_PORT", "5003")),
        ws_max_size=MAX_MESSAGE,
        ws_max_queue=4,
        log_config=None,
    )
