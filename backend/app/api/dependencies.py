"""HTTP authentication dependencies for JWT, admin, ownership and workspace checks.

Only Authorization Bearer is accepted; URL parameters are never identity
credentials. MCP-specific JWTs must still exist in the mcp_token table.
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
    """A request user and the factory that owns its SQL work."""

    user: User
    session_factory: SessionFactory


def _claims(encoded: str) -> Mapping[str, object]:
    return cast(
        "Mapping[str, object]",
        jwt.decode(encoded, SECRET_KEY, algorithms=["HS256"]),
    )


def _validate_token_scope(payload: Mapping[str, object]) -> None:
    """Reject malformed subjects and tokens issued for another HTTP surface."""
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
    """Resolve the request user from an Authorization Bearer header.

    MCP JWTs must still be active in the mcp_token table or they are rejected.
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
    """Bundle the authenticated user and its Session factory for routers."""
    return RequestContext(current_user, session_factory)


Ctx = Annotated[RequestContext, Depends(request_context)]


def require_admin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """Require an administrator role."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_ADMIN_REQUIRED,
        )
    return current_user


def admin_request_context(
    current_user: Annotated[User, Depends(require_admin)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> RequestContext:
    """Bundle an admin user and its Session factory for admin-only routers."""
    return RequestContext(current_user, session_factory)


AdminCtx = Annotated[RequestContext, Depends(admin_request_context)]


def ensure_owner_or_admin(current_user: User, owner_id: int) -> None:
    """Reject non-owners who are not administrators."""
    if int(current_user.id) != int(owner_id) and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_PERMISSION_DENIED,
        )


def _runtime_workspace(payload: Mapping[str, object]) -> int | None:
    """Return a runtime token's bound workspace, or None when it is malformed."""
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
    """Resolve X-Workspace-Id and verify the user is a member.

    File and folder operations require this header.
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

    # Runtime tokens are cryptographically bound to one workspace. A caller
    # may not reuse the token with a different HTTP workspace header.
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
    """Build a dependency that requires the current workspace role."""

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
