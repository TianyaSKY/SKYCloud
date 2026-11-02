"""用于 JWT、管理员、所有权及工作区检查的 HTTP 认证依赖项.

仅接受 Authorization Bearer 格式; URL 参数绝不能作为身份凭证.
MCP 专用的 JWT 必须在 mcp_token 表中处于活跃状态.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, cast

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.authentication import resolve_http_user
from app.features.workspace import permissions
from app.features.workspace.permissions import assert_member
from app.infra.extensions import SECRET_KEY, SessionFactory, get_session_factory
from app.infra.owned_work import run_owned
from app.models.user import User
from app.models.workspace import Workspace

bearer_scheme = HTTPBearer(auto_error=False)

_MISSING_CREDENTIAL = "Token is missing!"
_INVALID_CREDENTIAL = "Invalid token!"
_EXPIRED_CREDENTIAL = "Token expired!"
_AUTH_UNAVAILABLE = "Authentication service unavailable"
_RUNTIME_SCOPE = "Runtime token is only valid for the scoped MCP endpoint"
_INVALID_SCOPE = "Invalid token scope"
_ADMIN_REQUIRED = "Admin privilege required"
_PERMISSION_DENIED = "Permission denied"
_WORKSPACE_HEADER_REQUIRED = "X-Workspace-Id header is required"
_WORKSPACE_HEADER_INVALID = "Invalid X-Workspace-Id header"
_WORKSPACE_BOUND = "Runtime token is bound to another workspace"
_WORKSPACE_MISSING = "Workspace not found"


@dataclass(frozen=True, slots=True)
class RequestContext:
    """封装请求用户及其持有 SQL 操作的 Session 工厂."""

    user: User
    session_factory: SessionFactory


def _claims(encoded: str) -> Mapping[str, object]:
    return cast(
        "Mapping[str, object]",
        jwt.decode(encoded, SECRET_KEY, algorithms=["HS256"]),
    )


def _validate_token_scope(payload: Mapping[str, object]) -> None:
    """拒绝畸形主体以及针对其他 HTTP 接口表面签发的令牌."""
    user_id = payload.get("sub")
    try:
        valid_user_id = isinstance(user_id, str) and int(user_id) > 0
    except ValueError:
        valid_user_id = False
    if not valid_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIAL,
        )
    token_type = payload.get("type")
    if token_type in {"mcp_runtime", "runtime_llm"}:
        raise HTTPException(status_code=403, detail=_RUNTIME_SCOPE)
    if token_type not in {None, "mcp"}:
        raise HTTPException(status_code=403, detail=_INVALID_SCOPE)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme),
    ],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> User:
    """从 Authorization Bearer 请求头中解析当前请求用户.

    MCP JWT 必须在 mcp_token 表中处于有效激活状态, 否则会被拒绝.
    """
    token = None
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_MISSING_CREDENTIAL,
        )

    try:
        payload = _claims(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_EXPIRED_CREDENTIAL,
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIAL,
        ) from exc

    _validate_token_scope(payload)
    try:
        return await run_owned(resolve_http_user, session_factory, payload, token)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_AUTH_UNAVAILABLE,
        ) from exc


def request_context(
    current_user: Annotated[User, Depends(get_current_user)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> RequestContext:
    """将已认证的用户及其 Session 工厂绑定为路由上下文."""
    return RequestContext(current_user, session_factory)


Ctx = Annotated[RequestContext, Depends(request_context)]


def require_admin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """要求当前用户具备管理员角色权限."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_ADMIN_REQUIRED,
        )
    return current_user


def admin_request_context(
    current_user: Annotated[User, Depends(require_admin)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> RequestContext:
    """将管理员用户及其 Session 工厂绑定为仅限管理员路由的上下文."""
    return RequestContext(current_user, session_factory)


AdminCtx = Annotated[RequestContext, Depends(admin_request_context)]


def ensure_owner_or_admin(current_user: User, owner_id: int) -> None:
    """拒绝非资源所有者且非管理员的访问请求."""
    if int(current_user.id) != int(owner_id) and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_PERMISSION_DENIED,
        )


def _runtime_workspace(payload: Mapping[str, object]) -> int | None:
    """返回运行时令牌绑定的工作区 ID, 若格式不合规返回 None."""
    if payload.get("type") != "mcp_runtime":
        return None
    try:
        return int(cast("str | int", payload.get("workspace_id", -1)))
    except (TypeError, ValueError):
        return None


def get_current_workspace(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> Workspace:
    """解析 X-Workspace-Id 并验证当前用户是否为该工作区成员.

    文件和文件夹操作均需此请求头.
    """
    ws_id = request.headers.get("X-Workspace-Id")
    if not ws_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_WORKSPACE_HEADER_REQUIRED,
        )
    try:
        workspace_id = int(ws_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_WORKSPACE_HEADER_INVALID,
        ) from None

    # 运行时令牌通过密码学算法与特定工作区绑定。调用方
    # 不得将该令牌与不同的 HTTP 工作区头组合复用。
    raw_token = request.headers.get("Authorization", "")
    if raw_token.lower().startswith("bearer "):
        raw_token = raw_token.split(" ", 1)[1].strip()
    else:
        raw_token = ""
    if raw_token:
        try:
            payload = _claims(raw_token)
        except jwt.InvalidTokenError:
            payload = None
        if payload is not None:
            bound_workspace = _runtime_workspace(payload)
            if bound_workspace is not None and bound_workspace != workspace_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=_WORKSPACE_BOUND,
                )

    with session_factory() as owned:
        workspace = owned.get(Workspace, workspace_id)
        if not workspace:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_WORKSPACE_MISSING,
            )

        assert_member(owned, workspace_id, int(current_user.id))
        return workspace


def require_workspace_role(
    min_role: str,
) -> Callable[[Workspace, User, SessionFactory], Workspace]:
    """构建校验用户当前工作区角色的依赖项."""

    def checker(
        workspace: Annotated[Workspace, Depends(get_current_workspace)],
        current_user: Annotated[User, Depends(get_current_user)],
        session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> Workspace:
        with session_factory() as owned:
            role = permissions.get_member_role(
                owned, workspace.id, int(current_user.id),
            )
            if role is None or not permissions.check_role_level(role, min_role):
                message = f"Requires {min_role} role or higher"
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=message,
                )
            return workspace

    return checker
