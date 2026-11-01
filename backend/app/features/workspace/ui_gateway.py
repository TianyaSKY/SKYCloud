"""Dedicated UI ingress: only opaque runtime sessions, never web Bearer tokens."""

import asyncio
import json
import os
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Annotated, Protocol
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from runtime_common.http_decode import ACCEPT_ENCODING, content_encoding, decoded_bytes
from runtime_common.http_stream import (
    UpstreamLease,
    UpstreamResponse,
    closable,
    close_reader,
    drain_cleanup,
)
from runtime_common.http_wait import WaitPolicy, guarded_wait
from runtime_common.json_types import JsonObject
from runtime_common.socket_relay import (
    SocketSlots,
    direct_connect,
    pump,
    terminal_path,
)
from runtime_controller.client import auth_headers, controller_url
from starlette.requests import ClientDisconnect

from app.features.workspace import ui_tickets
from app.infra.extensions import SessionFactory, SessionLocal, get_session_factory
from app.infra.owned_work import run_owned

router = APIRouter(prefix="/runtime-ui", include_in_schema=False)
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
MAX_BODY_SECONDS = 30
MAX_HEADER_SECONDS = 90
MAX_QUERY_BYTES = 4096
HTTP_RECHECK_SECONDS = 1.0
RECHECK_SECONDS = 15
UPSTREAM_TIMEOUT_SECONDS = 90
UPSTREAM_CONNECT_SECONDS = 5
DEFAULT_APP_ORIGIN = "http://app.skycloud.localhost"
UPSTREAM_HEADERS = ("content-type", "accept")
ALLOWED_QUERY_KEYS = frozenset(
    {"directory", "location[directory]", "workspace", "cursor", "ticket"},
)
SAFE_METHODS = frozenset({"GET", "HEAD"})

CLIENT_DISCONNECTED = "Runtime UI client disconnected"
COOKIE_SCOPE_MISMATCH = "Runtime UI cookie scope mismatch"
ORIGIN_MISMATCH = "Runtime UI origin mismatch"
ORIGIN_REQUIRED = "Runtime UI origin required"
UNSUPPORTED_PATH = "Unsupported runtime UI path"
REQUEST_TOO_LARGE = "Runtime UI request too large"
BODY_TIMEOUT = "Runtime UI request timed out"
UPSTREAM_TIMEOUT = "Runtime UI upstream timed out"
UPSTREAM_UNAVAILABLE = "Runtime UI unavailable"

socket_slots = SocketSlots()


class RequestSurface(Protocol):
    """The request surface shared by HTTP and WebSocket scopes."""

    @property
    def headers(self) -> Mapping[str, str]:
        """Return the request headers."""
        ...

    @property
    def cookies(self) -> Mapping[str, str]:
        """Return the parsed request cookies."""
        ...


@dataclass(frozen=True, slots=True)
class RelayTarget:
    """Immutable upstream target of one UI relay request."""

    url: str
    headers: dict[str, str]
    body: bytes
    raw_credential: str
    expires_at: float


def _host(surface: RequestSurface) -> str:
    return surface.headers.get("host", "")


def _credential(surface: RequestSurface) -> str:
    return (
        surface.cookies.get("__Host-" + ui_tickets.COOKIE)
        or surface.cookies.get(ui_tickets.COOKIE)
        or ""
    )


def _app_origin() -> str:
    return os.getenv("APP_ORIGIN", DEFAULT_APP_ORIGIN).rstrip("/")


def authenticate_owned(
    session_factory: SessionFactory,
    raw: str,
    host: str,
) -> JsonObject:
    """Authenticate one UI session inside its owning worker thread."""
    with session_factory() as session:
        return ui_tickets.authenticate(session, raw, host)


def redeem_owned(
    session_factory: SessionFactory,
    raw: str,
    host: str,
) -> tuple[str, JsonObject]:
    """Redeem one UI ticket inside its owning worker thread."""
    with session_factory() as session:
        return ui_tickets.redeem(session, raw, host)


def reauthenticate(
    raw: str,
    host: str,
    session_factory: SessionFactory | None = None,
) -> JsonObject:
    """Check a live credential on a fresh Session owned by this call."""
    # Streaming responses outlive yield dependencies on supported FastAPI
    # versions; each periodic check owns and closes its independent Session.
    factory = SessionLocal if session_factory is None else session_factory
    return authenticate_owned(factory, raw, host)


async def _verify_credential(
    raw: str,
    host: str,
    session_factory: SessionFactory,
) -> None:
    await run_owned(reauthenticate, raw, host, session_factory)


def _require_connected(*, disconnected: bool) -> None:
    if disconnected:
        raise HTTPException(status_code=499, detail=CLIENT_DISCONNECTED)


async def _next_chunk(iterator: AsyncIterator[bytes]) -> bytes:
    return await anext(iterator)


@router.get("/__skycloud/launch")
async def launch(
    request: Request,
    ticket: str,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> RedirectResponse:
    """Redeem a launch ticket and set its host-bound session cookie."""
    raw, payload = await run_owned(
        redeem_owned,
        session_factory,
        ticket,
        _host(request),
    )
    origin = ui_tickets.credential_origin(payload)
    response = RedirectResponse(
        "/",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
    response.set_cookie(
        ui_tickets.cookie_name(origin),
        raw,
        max_age=ui_tickets.SESSION_TTL,
        httponly=True,
        secure=origin.startswith("https://"),
        samesite="strict",
        path="/",
    )
    return response


@router.get("/__skycloud/renew")
async def renew(
    request: Request,
    ticket: str,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> HTMLResponse:
    """Redeem a renewal ticket and acknowledge it to the parent frame."""
    raw, payload = await run_owned(
        redeem_owned,
        session_factory,
        ticket,
        _host(request),
    )
    origin = ui_tickets.credential_origin(payload)
    nonce = secrets.token_urlsafe(32)
    parent = _app_origin()
    # The acknowledgement contains only the already consumed ticket, never
    # the newly issued HttpOnly session. It identifies the exact pending frame.
    message = json.dumps(
        {"type": "skycloud-runtime-ui-renewed", "ticket": ticket},
    ).replace("<", "\\u003c")
    destination = json.dumps(parent).replace("<", "\\u003c")
    response = HTMLResponse(
        f'<!doctype html><script nonce="{nonce}">'
        f"window.parent.postMessage({message}, {destination});</script>",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                f"frame-ancestors {parent}"
            ),
        },
    )
    response.set_cookie(
        ui_tickets.cookie_name(origin),
        raw,
        max_age=ui_tickets.SESSION_TTL,
        httponly=True,
        secure=origin.startswith("https://"),
        samesite="strict",
        path="/",
    )
    return response


def _verify_request(
    path: str,
    request: Request,
    credential: ui_tickets.CredentialFields,
) -> None:
    """Enforce cookie scope, same-origin mutation and safe path shape."""
    if request.cookies.get(ui_tickets.cookie_name(credential.origin)) != _credential(
        request,
    ):
        raise HTTPException(status_code=401, detail=COOKIE_SCOPE_MISMATCH)
    # Same-site sibling runtimes must not be able to mutate another runtime.
    origin = request.headers.get("origin")
    if origin and origin != credential.origin:
        raise HTTPException(status_code=403, detail=ORIGIN_MISMATCH)
    if request.method not in SAFE_METHODS and origin != credential.origin:
        raise HTTPException(status_code=403, detail=ORIGIN_REQUIRED)
    if (
        "\\" in path
        or ":" in path
        or path.startswith("/")
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise HTTPException(status_code=403, detail=UNSUPPORTED_PATH)


async def _bounded_body(request: Request) -> bytes:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REQUEST:
                raise HTTPException(status_code=413, detail=REQUEST_TOO_LARGE)
            body.extend(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(status_code=499, detail=CLIENT_DISCONNECTED) from exc
    return bytes(body)


async def _read_authorized_body(
    request: Request,
    session_factory: SessionFactory,
    credential: ui_tickets.CredentialFields,
) -> bytes:
    async def verify() -> None:
        await _verify_credential(_credential(request), _host(request), session_factory)

    return await guarded_wait(
        _bounded_body(request),
        WaitPolicy(
            check=verify,
            seconds=max(
                0.001,
                min(MAX_BODY_SECONDS, credential.expires_at - time.time()),
            ),
            status=408,
            detail=BODY_TIMEOUT,
            interval=HTTP_RECHECK_SECONDS,
        ),
    )


def _relay_target(
    request: Request,
    path: str,
    body: bytes,
    credential: ui_tickets.CredentialFields,
) -> RelayTarget:
    url = (
        controller_url()
        + "/runtimes/"
        + quote(credential.container_id, safe="")
        + "/ui/"
        + quote(path, safe="/")
    )
    headers = {
        key: request.headers[key] for key in UPSTREAM_HEADERS if key in request.headers
    }
    if request.headers.get("x-opencode-ticket") == "1":
        headers["x-opencode-ticket"] = "1"
    headers.update(auth_headers())
    headers["Accept-Encoding"] = ACCEPT_ENCODING
    return RelayTarget(
        url=url,
        headers=headers,
        body=body,
        raw_credential=_credential(request),
        expires_at=credential.expires_at,
    )


async def _open_upstream(
    client: httpx.AsyncClient,
    request: Request,
    target: RelayTarget,
    lease: UpstreamLease,
    session_factory: SessionFactory,
) -> httpx.Response:
    async def open_response() -> httpx.Response:
        upstream = await client.send(
            client.build_request(
                request.method,
                target.url,
                headers=target.headers,
                content=target.body,
                params=list(request.query_params.multi_items()),
            ),
            stream=True,
        )
        lease.upstream = upstream
        return upstream

    async def check() -> None:
        _require_connected(disconnected=await request.is_disconnected())
        await _verify_credential(
            target.raw_credential,
            _host(request),
            session_factory,
        )

    return await guarded_wait(
        open_response(),
        WaitPolicy(
            check=check,
            seconds=max(
                0.001,
                min(MAX_HEADER_SECONDS, target.expires_at - time.time()),
            ),
            status=504,
            detail=UPSTREAM_TIMEOUT,
            interval=HTTP_RECHECK_SECONDS,
            on_abort=lease.close,
        ),
    )


async def _stream_output(
    upstream: httpx.Response,
    lease: UpstreamLease,
    request: Request,
    target: RelayTarget,
    session_factory: SessionFactory,
) -> AsyncGenerator[bytes, None]:
    size = 0
    pending: asyncio.Task[bytes] | None = None
    iterator = closable(decoded_bytes(upstream, MAX_RESPONSE).__aiter__())
    deadline = time.monotonic() + max(0, target.expires_at - time.time())
    failed = False
    try:
        while time.monotonic() < deadline:
            if pending is None:
                pending = asyncio.create_task(_next_chunk(iterator))
            ready, _ = await asyncio.wait(
                {pending},
                timeout=max(
                    0.001,
                    min(HTTP_RECHECK_SECONDS, deadline - time.monotonic()),
                ),
            )
            # Recheck after the read, before any newly arrived bytes leave
            # this gateway; quiet upstreams also get periodic checks.
            _require_connected(disconnected=await request.is_disconnected())
            await _verify_credential(
                target.raw_credential,
                _host(request),
                session_factory,
            )
            if not ready:
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                break
            pending = None
            size += len(chunk)
            if size > MAX_RESPONSE or time.monotonic() >= deadline:
                break
            yield chunk
    except (httpx.HTTPError, HTTPException):
        return
    except BaseException:
        failed = True
        raise
    finally:

        async def release() -> None:
            await close_reader(pending, iterator)
            await lease.close()

        cancelled = await drain_cleanup(release())
        if cancelled and not failed:
            raise asyncio.CancelledError


async def _relay_upstream(
    request: Request,
    session_factory: SessionFactory,
    target: RelayTarget,
) -> UpstreamResponse:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            UPSTREAM_TIMEOUT_SECONDS,
            connect=UPSTREAM_CONNECT_SECONDS,
        ),
        trust_env=False,
        follow_redirects=False,
    )
    lease = UpstreamLease(client)
    try:
        upstream = await _open_upstream(client, request, target, lease, session_factory)
        content_encoding(upstream)
    except BaseException as exc:
        await lease.close()
        if isinstance(exc, httpx.HTTPError):
            raise HTTPException(status_code=502, detail=UPSTREAM_UNAVAILABLE) from exc
        raise
    return UpstreamResponse(
        _stream_output(upstream, lease, request, target, session_factory),
        lease=lease,
        status_code=upstream.status_code,
        headers={
            "Content-Type": upstream.headers.get(
                "content-type",
                "application/octet-stream",
            ),
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "frame-ancestors " + _app_origin(),
        },
    )


@router.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
)
async def relay(
    path: str,
    request: Request,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> UpstreamResponse:
    """Relay one authenticated same-origin request to its runtime."""
    payload = await run_owned(
        authenticate_owned,
        session_factory,
        _credential(request),
        _host(request),
    )
    credential = ui_tickets.credential_fields(payload)
    _verify_request(path, request, credential)
    body = await _read_authorized_body(request, session_factory, credential)
    target = _relay_target(request, path, body, credential)
    return await _relay_upstream(request, session_factory, target)


def _terminal_allowed(
    path: str,
    websocket: WebSocket,
    credential: ui_tickets.CredentialFields,
) -> bool:
    """Whether this socket path, query and cookie set may be bridged."""
    return (
        websocket.cookies.get(ui_tickets.cookie_name(credential.origin))
        == _credential(websocket)
        and websocket.headers.get("origin") == credential.origin
        and terminal_path(path)
        and len(websocket.scope.get("query_string", b"")) <= MAX_QUERY_BYTES
        and all(key in ALLOWED_QUERY_KEYS for key in websocket.query_params)
    )


async def _bridge_socket(
    path: str,
    websocket: WebSocket,
    session_factory: SessionFactory,
    credential: ui_tickets.CredentialFields,
) -> None:
    url = controller_url()
    url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    url += "/runtimes/" + quote(credential.container_id, safe="") + "/ws/" + path
    query = urlencode(list(websocket.query_params.multi_items()))
    if query:
        url += "?" + query

    async def guard() -> None:
        await run_owned(
            reauthenticate,
            _credential(websocket),
            _host(websocket),
            session_factory,
        )

    async with direct_connect(url, auth_headers()) as upstream:
        await websocket.accept()
        await pump(
            websocket,
            upstream,
            seconds=credential.expires_at - time.time(),
            guard=guard,
            interval=RECHECK_SECONDS,
        )


@router.websocket("/{path:path}")
async def terminal(
    path: str,
    websocket: WebSocket,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> None:
    """Bridge one validated UI WebSocket to its runtime terminal."""
    acquired = False
    owner: int | None = None
    try:
        payload = await run_owned(
            reauthenticate,
            _credential(websocket),
            _host(websocket),
            session_factory,
        )
        credential = ui_tickets.credential_fields(payload)
        if not _terminal_allowed(path, websocket, credential):
            await websocket.close(code=1008)
            return
        owner = credential.user_id
        if not socket_slots.acquire(str(owner)):
            await websocket.close(code=1013)
            return
        acquired = True
        await _bridge_socket(path, websocket, session_factory, credential)
    except asyncio.CancelledError:
        raise
    except HTTPException:
        with suppress(RuntimeError):
            await websocket.close(code=1008)
    except Exception:
        # Transport failures must still close the socket without leaking text.
        with suppress(RuntimeError):
            await websocket.close(code=1011)
    finally:
        if acquired and owner is not None:
            socket_slots.release(str(owner))
