"""Dedicated UI ingress: only opaque runtime sessions, never web Bearer tokens."""
import asyncio
import time
import os
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.infra.extensions import get_db, SessionLocal
from app.features.workspace import ui_tickets
from runtime_controller.client import auth_headers, controller_url

router = APIRouter(prefix='/runtime-ui', include_in_schema=False)
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
RECHECK_SECONDS = 15


def _host(request):
    return request.headers.get('host', '')


def _credential(request):
    return request.cookies.get('__Host-' + ui_tickets.COOKIE) or request.cookies.get(ui_tickets.COOKIE) or ''


def reauthenticate(raw, host):
    # Streaming responses outlive yield dependencies on supported FastAPI
    # versions; each periodic check owns and closes its independent Session.
    with SessionLocal() as session:
        return ui_tickets.authenticate(session, raw, host)


@router.get('/__skycloud/launch')
def launch(request: Request, ticket: str, session: Session = Depends(get_db)):
    raw, payload = ui_tickets.redeem(session, ticket, _host(request))
    response = RedirectResponse('/', status_code=303, headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
    response.set_cookie(ui_tickets.cookie_name(payload['origin']), raw,
                        max_age=ui_tickets.SESSION_TTL, httponly=True,
                        secure=payload['origin'].startswith('https://'), samesite='strict', path='/')
    return response


@router.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'])
async def relay(path: str, request: Request, session: Session = Depends(get_db)):
    payload = await run_in_threadpool(ui_tickets.authenticate, session, _credential(request), _host(request))
    if request.cookies.get(ui_tickets.cookie_name(payload['origin'])) != _credential(request):
        raise HTTPException(401, 'Runtime UI cookie scope mismatch')
    # Same-site sibling runtimes must not be able to mutate another runtime.
    origin = request.headers.get('origin')
    if origin and origin != payload['origin']:
        raise HTTPException(403, 'Runtime UI origin mismatch')
    if request.method not in {'GET', 'HEAD'} and origin != payload['origin']:
        raise HTTPException(403, 'Runtime UI origin required')
    if ('\\' in path or ':' in path or path.startswith('/')
            or any(part in {'.', '..'} for part in path.split('/'))):
        raise HTTPException(403, 'Unsupported runtime UI path')
    async def bounded_body():
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REQUEST:
                raise HTTPException(413, 'Runtime UI request too large')
            body.extend(chunk)
        return bytes(body)
    started = time.monotonic()
    try:
        body = await asyncio.wait_for(bounded_body(),
            timeout=max(0.001, min(30, payload['expires_at'] - time.time())))
    except asyncio.TimeoutError as exc:
        raise HTTPException(408, 'Runtime UI request timed out') from exc
    if request.method not in {'GET', 'HEAD'} or time.monotonic() - started >= RECHECK_SECONDS:
        payload = await run_in_threadpool(reauthenticate, _credential(request), _host(request))
    url = controller_url() + '/runtimes/' + quote(payload['container_id'], safe='') + '/ui/' + quote(path, safe='/')
    headers = {key: request.headers[key] for key in ('content-type', 'accept') if key in request.headers}
    headers.update(auth_headers())
    client = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5), trust_env=False, follow_redirects=False)
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers,
                    content=body, params=list(request.query_params.multi_items())), stream=True)
    except BaseException as exc:
        await client.aclose()
        if isinstance(exc, httpx.HTTPError):
            raise HTTPException(502, 'Runtime UI unavailable') from exc
        raise
    async def output():
        size = 0
        pending = None
        iterator = upstream.aiter_bytes().__aiter__()
        deadline = time.monotonic() + max(0, payload['expires_at'] - time.time())
        check_at = time.monotonic() + RECHECK_SECONDS
        try:
            while time.monotonic() < deadline:
                if time.monotonic() >= check_at:
                    await run_in_threadpool(reauthenticate, _credential(request), _host(request))
                    check_at = time.monotonic() + RECHECK_SECONDS
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                try:
                    chunk = await asyncio.wait_for(asyncio.shield(pending),
                        timeout=max(0.001, min(RECHECK_SECONDS, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    continue
                except StopAsyncIteration:
                    break
                pending = None
                size += len(chunk)
                if size > MAX_RESPONSE or time.monotonic() >= deadline:
                    break
                yield chunk
        except (httpx.HTTPError, HTTPException):
            return
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await upstream.aclose()
            await client.aclose()
    return StreamingResponse(output(), status_code=upstream.status_code, headers={
        'Content-Type': upstream.headers.get('content-type', 'application/octet-stream'),
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': 'frame-ancestors ' + os.getenv('APP_ORIGIN', 'http://app.skycloud.localhost').rstrip('/'),
    })
