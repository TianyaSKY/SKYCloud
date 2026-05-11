"""Workspace router — CRUD + reverse proxy (HTTP & WebSocket) for opencode containers."""

import asyncio
import base64
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import StreamingResponse, Response

from app.dependencies import get_current_user
from app.models.user import User
from app.services import workspace_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workspace"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class WorkspaceCreateRequest(BaseModel):
    name: str = Field(default="My Workspace", max_length=120)


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.post("/workspace")
async def create_workspace(
    payload: WorkspaceCreateRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        ws = workspace_service.create_workspace(current_user.id, payload.name)
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=ws.to_dict(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/workspace")
async def list_workspaces(current_user: User = Depends(get_current_user)):
    workspaces = workspace_service.list_workspaces(current_user.id)
    return {"workspaces": workspaces, "code": 200}


@router.get("/workspace/{workspace_id}")
async def get_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
):
    ws = workspace_service.get_workspace(workspace_id, current_user.id)
    if not ws:
        raise HTTPException(status_code=404, detail="工作区不存在")
    return ws.to_dict()


@router.post("/workspace/{workspace_id}/start")
async def start_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
):
    try:
        ws = workspace_service.start_workspace(workspace_id, current_user.id)
        return ws.to_dict()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="工作区不存在")


@router.post("/workspace/{workspace_id}/stop")
async def stop_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
):
    try:
        ws = workspace_service.stop_workspace(workspace_id, current_user.id)
        return ws.to_dict()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="工作区不存在")


@router.delete("/workspace/{workspace_id}")
async def delete_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
):
    try:
        workspace_service.delete_workspace(workspace_id, current_user.id)
        return JSONResponse(status_code=200, content={"message": "已删除", "code": 200})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="工作区不存在")


@router.post("/workspace/{workspace_id}/restart")
async def restart_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
):
    try:
        ws = workspace_service.restart_workspace(workspace_id, current_user.id)
        return ws.to_dict()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="工作区不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/workspace/{workspace_id}/setup-mcp")
async def setup_mcp_connection(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
):
    try:
        result = workspace_service.setup_mcp_connection(workspace_id, current_user.id)
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="工作区不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Reverse proxy helpers
# ---------------------------------------------------------------------------

# Shared httpx client with longer timeout for AI responses
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    return _http_client


def _make_auth_header(password: str) -> str:
    return "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()


PROXY_COOKIE_NAME = "skycloud_ws_token"


async def _resolve_proxy_user(request: Request) -> User:
    """Resolve the current user for proxy requests.

    Priority:
    1. ?token= query parameter (used by iframe initial load)
    2. skycloud_ws_token cookie (set after first successful auth, used by
       subsequent resource loads inside the iframe)
    3. Authorization header (standard Bearer token)
    """
    # 1. Query parameter
    effective_token = request.query_params.get("token")

    # 2. Cookie fallback
    if not effective_token:
        effective_token = request.cookies.get(PROXY_COOKIE_NAME)

    # 3. Use the standard get_current_user dependency logic
    if effective_token:
        from fastapi.security import HTTPAuthorizationCredentials
        creds = HTTPAuthorizationCredentials(scheme="bearer", credentials=effective_token)
        return await get_current_user(credentials=creds, token=None)

    # 4. Try Authorization header
    return await get_current_user(credentials=None, token=None)


# ---------------------------------------------------------------------------
# Reverse proxy — HTTP
# ---------------------------------------------------------------------------


@router.api_route(
    "/workspace/{workspace_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_http(
    workspace_id: int,
    path: str,
    request: Request,
):
    import traceback
    try:
        return await _proxy_http_inner(workspace_id, path, request)
    except HTTPException:
        raise
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("proxy_http error: %s\n%s", exc, tb)
        raise HTTPException(status_code=500, detail=f"Proxy error: {exc}")


async def _proxy_http_inner(
    workspace_id: int,
    path: str,
    request: Request,
):
    # Authenticate via token/cookie/header
    try:
        current_user = await _resolve_proxy_user(request)
    except HTTPException:
        raise HTTPException(status_code=401, detail="认证失败，请重新打开工作区")

    ws = workspace_service.get_workspace(workspace_id, current_user.id)
    if not ws:
        raise HTTPException(status_code=404, detail="工作区不存在")
    if ws.status != "running":
        raise HTTPException(status_code=400, detail="工作区未运行")

    container_url = workspace_service.get_container_url(ws)
    target_url = f"{container_url}/{path}"

    # Forward query params but strip our auth token
    query_params = dict(request.query_params)
    query_params.pop("token", None)
    if query_params:
        qs = "&".join(f"{k}={v}" for k, v in query_params.items())
        target_url += f"?{qs}"

    # Build clean headers for upstream — only forward essentials
    headers = {
        "host": container_url.split("//")[1],
    }
    # Forward content-type for POST/PUT
    ct = request.headers.get("content-type")
    if ct:
        headers["content-type"] = ct
    accept = request.headers.get("accept")
    if accept:
        headers["accept"] = accept

    client = _get_http_client()
    body = await request.body()

    try:
        resp = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="工作区容器连接失败")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="工作区容器响应超时")

    # Build response headers — skip hop-by-hop and restrictive security headers
    resp_headers = {}
    skip = {
        "transfer-encoding", "connection", "content-encoding", "content-length",
        # Remove upstream CSP — it uses 'self' which refers to the container
        # origin, not the SKYCloud origin, breaking inline scripts and resource
        # loading through the proxy
        "content-security-policy",
        "x-frame-options",  # Allow embedding in our iframe
    }
    for k, v in resp.headers.items():
        if k.lower() not in skip:
            resp_headers[k] = v

    content = resp.content
    content_type = resp.headers.get("content-type", "")
    proxy_base = f"/api/workspace/{workspace_id}/proxy"

    # Rewrite HTML to prefix absolute resource paths with the proxy path
    if "text/html" in content_type:
        html = content.decode("utf-8", errors="replace")
        # Rewrite absolute paths in src/href attributes to go through proxy
        import re
        # Match src="/ or href="/ but NOT src="//" (protocol-relative URLs)
        html = re.sub(
            r'((?:src|href|action)\s*=\s*["\'])(/(?!/))([^"\']*)',
            rf'\1{proxy_base}/\3',
            html,
        )
        # Also rewrite manifest and other JSON-like references
        html = re.sub(
            r'(content\s*=\s*["\'])(/(?!/))([^"\']*)',
            rf'\1{proxy_base}/\3',
            html,
        )
        content = html.encode("utf-8")

    response = Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp.headers.get("content-type"),
    )

    # Set auth cookie on root path so all iframe sub-requests are authenticated
    jwt_token = request.query_params.get("token") or request.cookies.get(PROXY_COOKIE_NAME)
    if jwt_token:
        response.set_cookie(
            key=PROXY_COOKIE_NAME,
            value=jwt_token,
            path="/",
            httponly=True,
            samesite="lax",
            max_age=86400,  # 24 hours
        )

    return response


# ---------------------------------------------------------------------------
# Reverse proxy — WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/workspace/{workspace_id}/proxy/{path:path}")
async def proxy_websocket(
    websocket: WebSocket,
    workspace_id: int,
    path: str,
):
    """Bi-directional WebSocket proxy to the opencode container.

    Auth is validated via the JWT token query parameter or cookie since
    browsers cannot send custom headers on WebSocket upgrade requests.
    """
    # Manual auth for WebSocket (can't use Depends)
    token = websocket.query_params.get("token") or websocket.cookies.get(PROXY_COOKIE_NAME)
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return
    try:
        from app.dependencies import get_current_user as _get_user
        from fastapi.security import HTTPAuthorizationCredentials

        creds = HTTPAuthorizationCredentials(scheme="bearer", credentials=token)
        current_user = await _get_user(credentials=creds, token=None)
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    ws = workspace_service.get_workspace(workspace_id, current_user.id)
    if not ws or ws.status != "running":
        await websocket.close(code=4002, reason="Workspace not running")
        return

    container_url = workspace_service.get_container_url(ws)
    container_ws_url = container_url.replace("http://", "ws://")
    target = f"{container_ws_url}/{path}"
    # Strip auth token from forwarded query
    query_params = dict(websocket.query_params)
    query_params.pop("token", None)
    if query_params:
        qs = "&".join(f"{k}={v}" for k, v in query_params.items())
        target += f"?{qs}"

    auth_header = _make_auth_header(ws.container_password)

    await websocket.accept()

    try:
        import websockets
        async with websockets.connect(
            target,
            additional_headers={"Authorization": auth_header},
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await upstream.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    async for message in upstream:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as exc:
        logger.warning("WebSocket proxy error: %s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
