"""Request authentication: resolve current SQL scope on a worker, then install/reset context."""
from app.infra.owned_work import run_owned
from app.mcp.unit_of_work import worker_session
import jwt
from app.infra.extensions import SECRET_KEY, SessionLocal
from app.features.auth.service import decode_token
from app.features.workspace.permissions import get_member_role
from app.mcp import runtime_token_service
from app.mcp.context import (
    McpRequestContext, _current_user_id, set_mcp_context, reset_mcp_context,
    set_mcp_request_active, reset_mcp_request_active, set_request_id, reset_request_id,
)


class JWTAuthMiddleware:
    """Resolve a verified user + workspace context for every MCP request.

    A normal session/MCP token selects a workspace through
    ``X-SKYCloud-Workspace-Id`` and membership lookup.  A runtime token carries
    its workspace in signed claims, but the runtime and current membership are
    still checked in the database.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _resolve_context(
            session,
            token: str,
            workspace_header: str | None,
    ) -> McpRequestContext | None:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return None

        if payload.get("type") == "mcp_runtime":
            validated = runtime_token_service.validate_runtime_token(session, token)
            if not validated:
                return None
            runtime_payload, runtime = validated
            try:
                workspace_id = int(runtime_payload["workspace_id"])
            except (KeyError, TypeError, ValueError):
                return None
            if workspace_header is not None:
                try:
                    if int(workspace_header) != workspace_id:
                        return None
                except (TypeError, ValueError):
                    return None
            return McpRequestContext(
                user_id=int(runtime.user_id),
                workspace_id=workspace_id,
                role=str(runtime_payload.get("role", "viewer")),
                runtime_id=int(runtime.id),
                actor_type="opencode",
            )

        result = decode_token(session, token)
        if not result or not str(result).isdigit() or workspace_header is None:
            return None
        try:
            user_id = int(result)
            workspace_id = int(workspace_header)
        except (TypeError, ValueError):
            return None

        role = get_member_role(session, workspace_id, user_id)
        if role is None:
            return None
        return McpRequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            role=role.value,
            actor_type="user",
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
        }
        auth_value = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        workspace_header = headers.get(b"x-skycloud-workspace-id")
        workspace_header_value = (
            workspace_header.decode("utf-8", errors="ignore")
            if workspace_header is not None
            else None
        )

        user_id: int | None = None
        context: McpRequestContext | None = None

        if auth_value.lower().startswith("bearer "):
            token = auth_value.split(" ", 1)[1].strip()
            if token:
                def authenticate():
                    with worker_session(SessionLocal) as session:
                        resolved = self._resolve_context(session, token, workspace_header_value)
                        if resolved:
                            return resolved, resolved.user_id
                        # Diagnostic identity never authorizes a real tool when
                        # no verified workspace context could be resolved.
                        result = decode_token(session, token)
                        return None, int(result) if result and str(result).isdigit() else None
                context, user_id = await run_owned(authenticate)

        request_active_ctx = set_mcp_request_active(True)
        token_ctx = _current_user_id.set(user_id)
        context_ctx = set_mcp_context(context)
        request_id_ctx = set_request_id(
            headers.get(b"x-request-id", b"").decode("utf-8", errors="ignore") or None
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_id(request_id_ctx)
            reset_mcp_context(context_ctx)
            _current_user_id.reset(token_ctx)
            reset_mcp_request_active(request_active_ctx)
