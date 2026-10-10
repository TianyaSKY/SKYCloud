"""HTTP 鉴权依赖：JWT 解析、管理员校验、资源归属检查、工作空间上下文。

仅接受 Authorization Bearer；URL 参数不能用作身份凭据。
MCP 专用 JWT 额外校验库内是否仍有效（吊销后立即拒绝）。
"""

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.infra.owned_work import run_owned
from sqlalchemy.orm import Session

from app.infra.extensions import SECRET_KEY, get_db, get_session_factory
from app.api.authentication import resolve_http_user
from fastapi.params import Depends as DependsParameter
from contextlib import nullcontext
from app.models.user import User
from app.models.workspace import Workspace
from app.features.auth import user_service
from app.features.workspace.permissions import assert_member, check_role_level

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        session: Session = Depends(get_db),
        session_factory=Depends(get_session_factory),
) -> User:
    """解析当前请求用户；仅接受 Bearer 请求头。

    MCP type 的 JWT 必须在 mcp_token 表中仍为 active，否则按未授权处理。
    """
    token = None
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is missing!"
        )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("sub")
        try:
            valid_user_id = isinstance(user_id, str) and int(user_id) > 0
        except ValueError:
            valid_user_id = False
        if not valid_user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token!"
            )
        token_type = payload.get("type")
        if token_type in {"mcp_runtime", "runtime_llm"}:
            raise HTTPException(status_code=403, detail="Runtime token is only valid for the scoped MCP endpoint")
        if token_type not in {None, "mcp"}:
            raise HTTPException(status_code=403, detail="Invalid token scope")
        # Direct legacy service tests may explicitly supply a borrowed Session.
        # ASGI dependency resolution always supplies the real factory instead.
        factory = (lambda: nullcontext(session)) if isinstance(session_factory, DependsParameter) else session_factory
        current_user = await run_owned(resolve_http_user, factory, payload, token)

        return current_user
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired!"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token!"
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authentication service unavailable"
        ) from exc


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """要求管理员角色，用于系统字典、全站 Token 用量等接口。"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required"
        )
    return current_user


def ensure_owner_or_admin(current_user: User, owner_id: int) -> None:
    """非资源所有者且非管理员时拒绝，避免越权读写他人数据。"""
    if int(current_user.id) != int(owner_id) and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied"
        )


# ---------------------------------------------------------------------------
# 工作空间上下文依赖
# ---------------------------------------------------------------------------


def get_current_workspace(
        request: Request,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
        session_factory=Depends(get_session_factory),
) -> Workspace:
    """从 X-Workspace-Id header 解析当前工作空间，校验用户为成员。

    前端所有文件/文件夹操作均需携带此 header。
    """
    ws_id = request.headers.get("X-Workspace-Id")
    if not ws_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Workspace-Id header is required",
        )
    try:
        workspace_id = int(ws_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Workspace-Id header",
        )

    # Runtime tokens are cryptographically bound to one workspace. A caller
    # may not reuse the token with a different HTTP workspace header.
    raw_token = request.headers.get("Authorization", "")
    if raw_token.lower().startswith("bearer "):
        raw_token = raw_token.split(" ", 1)[1].strip()
    else:
        raw_token = ""
    if raw_token:
        try:
            payload = jwt.decode(raw_token, SECRET_KEY, algorithms=["HS256"])
            if payload.get("type") == "mcp_runtime":
                if int(payload.get("workspace_id", -1)) != workspace_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Runtime token is bound to another workspace",
                    )
        except HTTPException:
            raise
        except (jwt.InvalidTokenError, TypeError, ValueError):
            # get_current_user is responsible for the canonical 401 response
            # for invalid credentials; do not mask it here.
            pass

    factory = (lambda: nullcontext(session)) if isinstance(session_factory, DependsParameter) else session_factory
    with factory() as owned:
        workspace = owned.get(Workspace, workspace_id)
        if not workspace:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Workspace not found",
            )

        # 校验用户为工作空间成员
        assert_member(owned, workspace_id, int(current_user.id))
        return workspace


def require_workspace_role(min_role: str):
    """依赖工厂：校验当前用户在当前工作空间的最低角色。

    用法：Depends(require_workspace_role("editor"))
    """

    def checker(
            workspace: Workspace = Depends(get_current_workspace),
            current_user: User = Depends(get_current_user),
            session: Session = Depends(get_db),
            session_factory=Depends(get_session_factory),
    ) -> Workspace:
        from app.features.workspace.permissions import get_member_role
        factory = (lambda: nullcontext(session)) if isinstance(session_factory, DependsParameter) else session_factory
        with factory() as owned:
            role = get_member_role(owned, workspace.id, int(current_user.id))
            if role is None or not check_role_level(role, min_role):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Requires {min_role} role or higher",
                )
            return workspace

    return checker
