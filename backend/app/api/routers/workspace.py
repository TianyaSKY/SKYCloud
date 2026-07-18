"""工作区路由：OpenCode 容器 CRUD 与 HTTP/WebSocket 反向代理。

生命周期与 Docker 操作在 workspace_service；本层只做鉴权、代理改写与错误映射。
"""

import asyncio
import base64
from dataclasses import asdict

import httpx
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.requests import Request
from starlette.responses import Response

from app.api.dependencies import get_current_user
from app.api.schemas.workspace import WorkspaceCreateRequest
from app.models.user import User
from app.services import workspace_service
from app.services.workspace_types import CreateWorkspaceCommand

router = APIRouter(tags=["workspace"])


# ---------------------------------------------------------------------------
# 工作区 CRUD
# ---------------------------------------------------------------------------


@router.post("/workspace")
async def create_workspace(
        payload: WorkspaceCreateRequest,
        current_user: User = Depends(get_current_user),
):
    """创建工作区记录（不立即启动容器）。"""
    workspace = workspace_service.create_workspace(
        CreateWorkspaceCommand(user_id=current_user.id, name=payload.name)
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=asdict(workspace_service.to_summary(workspace)),
    )


@router.get("/workspace")
async def list_workspaces(current_user: User = Depends(get_current_user)):
    """列出当前用户的工作区摘要。"""
    workspaces = workspace_service.list_workspaces(current_user.id)
    return {"workspaces": [asdict(workspace) for workspace in workspaces], "code": 200}


@router.get("/workspace/{workspace_id}")
async def get_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
):
    """获取单个工作区；不存在时 404。"""
    ws = workspace_service.get_workspace(workspace_id, current_user.id)
    if not ws:
        from app.exceptions import ResourceNotFoundError
        raise ResourceNotFoundError("工作区不存在")
    return asdict(workspace_service.to_summary(ws))


@router.post("/workspace/{workspace_id}/start")
async def start_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
):
    """启动 OpenCode 容器。"""
    workspace = workspace_service.start_workspace(workspace_id, current_user.id)
    return asdict(workspace_service.to_summary(workspace))


@router.post("/workspace/{workspace_id}/stop")
async def stop_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
):
    """停止容器（保留工作区记录）。"""
    workspace = workspace_service.stop_workspace(workspace_id, current_user.id)
    return asdict(workspace_service.to_summary(workspace))


@router.delete("/workspace/{workspace_id}")
async def delete_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
):
    """销毁容器并删除工作区记录。"""
    workspace_service.delete_workspace(workspace_id, current_user.id)
    return JSONResponse(status_code=200, content={"message": "已删除", "code": 200})


@router.post("/workspace/{workspace_id}/restart")
async def restart_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
):
    """重启容器（配置变更后常用）。"""
    workspace = workspace_service.restart_workspace(workspace_id, current_user.id)
    return asdict(workspace_service.to_summary(workspace))


@router.post("/workspace/{workspace_id}/setup-mcp")
async def setup_mcp_connection(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
):
    """向运行中工作区写入 MCP 连接配置（含当前用户 JWT）。"""
    result = workspace_service.setup_mcp_connection(workspace_id, current_user.id)
    return {"success": True, "message": "MCP 连接配置成功", **asdict(result)}


# ---------------------------------------------------------------------------
# 反向代理辅助
# ---------------------------------------------------------------------------

# 复用 AsyncClient；超时放宽以兼容 AI 长响应
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """懒创建全局 httpx 客户端（连接超时 10s，总超时 120s）。"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    return _http_client


def _make_auth_header(password: str) -> str:
    """构造容器侧 Basic Auth（用户名固定 opencode）。"""
    return "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()


PROXY_COOKIE_NAME = "skycloud_ws_token"


async def _resolve_proxy_user(request: Request) -> User:
    """解析代理请求用户。

    优先级：query ``token``（iframe 首屏）→ Cookie（后续子资源）→ Authorization Bearer。
    浏览器 iframe 无法为每个静态资源带自定义头，故需 Cookie 回落。
    """
    effective_token = request.query_params.get("token")

    if not effective_token:
        effective_token = request.cookies.get(PROXY_COOKIE_NAME)

    if effective_token:
        from fastapi.security import HTTPAuthorizationCredentials
        creds = HTTPAuthorizationCredentials(scheme="bearer", credentials=effective_token)
        return await get_current_user(credentials=creds, token=None)

    return await get_current_user(credentials=None, token=None)


# ---------------------------------------------------------------------------
# HTTP 反向代理
# ---------------------------------------------------------------------------


@router.api_route(
    "/workspace/{workspace_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_http(
        workspace_id: int,
        path: str,
        request: Request,
):
    """将 HTTP 请求转发到 OpenCode 容器；外层统一记日志。"""
    import traceback
    try:
        return await _proxy_http_inner(workspace_id, path, request)
    except HTTPException:
        raise
    except Exception as exc:
        tb = traceback.format_exc()
        logger.exception("工作区 HTTP 代理错误：{}\n{}", exc, tb)
        raise HTTPException(status_code=500, detail="Proxy error") from exc


async def _proxy_http_inner(
        workspace_id: int,
        path: str,
        request: Request,
):
    """代理实现：鉴权 → 校验 running → 转发 → 改写 HTML 资源路径 → 种鉴权 Cookie。"""
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

    # 鉴权 token 仅供代理层，不得转发给容器
    query_params = dict(request.query_params)
    query_params.pop("token", None)
    if query_params:
        qs = "&".join(f"{k}={v}" for k, v in query_params.items())
        target_url += f"?{qs}"

    # 只转发业务必需头，避免 Host/Cookie 污染上游
    headers = {
        "host": container_url.split("//")[1],
    }
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

    # 剥离逐跳头；去掉 CSP/XFO，否则 'self' 指向容器源，iframe 内脚本与嵌套会失败
    resp_headers = {}
    skip = {
        "transfer-encoding", "connection", "content-encoding", "content-length",
        "content-security-policy",
        "x-frame-options",
    }
    for k, v in resp.headers.items():
        if k.lower() not in skip:
            resp_headers[k] = v

    content = resp.content
    content_type = resp.headers.get("content-type", "")
    proxy_base = f"/api/workspace/{workspace_id}/proxy"

    # HTML 内绝对路径需改写到代理前缀，否则浏览器会打到 SKYCloud 根路径
    if "text/html" in content_type:
        html = content.decode("utf-8", errors="replace")
        import re
        # 匹配 src="/ 或 href="/，排除协议相对 //
        html = re.sub(
            r'((?:src|href|action)\s*=\s*["\'])(/(?!/))([^"\']*)',
            rf'\1{proxy_base}/\3',
            html,
        )
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

    # 首屏带 token 时写入 Cookie，供 iframe 内后续无头请求复用
    jwt_token = request.query_params.get("token") or request.cookies.get(PROXY_COOKIE_NAME)
    if jwt_token:
        response.set_cookie(
            key=PROXY_COOKIE_NAME,
            value=jwt_token,
            path="/",
            httponly=True,
            samesite="lax",
            max_age=86400,  # 24 小时
        )

    return response


# ---------------------------------------------------------------------------
# WebSocket 反向代理
# ---------------------------------------------------------------------------

@router.websocket("/workspace/{workspace_id}/proxy/{path:path}")
async def proxy_websocket(
        websocket: WebSocket,
        workspace_id: int,
        path: str,
):
    """双向 WebSocket 代理到 OpenCode 容器。

    浏览器升级请求无法带自定义头，故仅用 query/Cookie 中的 JWT 鉴权；
    上游 Basic Auth 使用容器密码，且不转发客户端 token。
    """
    # WebSocket 路由无法使用 Depends，须手动鉴权
    token = websocket.query_params.get("token") or websocket.cookies.get(PROXY_COOKIE_NAME)
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return
    try:
        from app.api.dependencies import get_current_user as _get_user
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
    # 鉴权 token 仅供代理层
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
        logger.warning("工作区 WebSocket 代理错误：{}", exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
