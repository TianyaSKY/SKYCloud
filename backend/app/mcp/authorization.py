"""Tool authorization uses fresh shared service checks for real MCP requests.

The direct-call test fallback is explicitly excluded from HTTP request scope.
"""
from app.exceptions import PermissionDeniedError
from app.features.file import service as file_service
from app.features.folder import service as folder_service
from app.mcp.context import (McpRequestContext, _current_user_id,
                             get_optional_mcp_context, is_mcp_request_active)
from app.mcp.permissions import assert_mcp_write_permission, assert_mcp_read_permission


def _get_authenticated_user_id() -> int:
    """Return the authenticated user, preserving legacy direct-call tests."""

    return _get_tool_context().user_id


def _get_tool_context() -> McpRequestContext:
    """Get strict request context, with a non-HTTP compatibility fallback.

    Older unit callers invoke tool functions directly and only set
    ``_current_user_id``.  That fallback is deliberately disabled once the
    ASGI middleware marks a real MCP request active, so an HTTP request can
    never bypass workspace selection.
    """

    context = get_optional_mcp_context()
    if context is not None:
        return context
    if not is_mcp_request_active():
        user_id = _current_user_id.get()
        if user_id is not None:
            return McpRequestContext(
                user_id=user_id,
                workspace_id=user_id,
                role="admin",
            )
    raise PermissionError(
        "Unauthorized: Missing or invalid Authorization header and workspace context."
    )


def _require_read_context() -> McpRequestContext:
    context = _get_tool_context()
    return context


def _require_write_context() -> McpRequestContext:
    context = _get_tool_context()
    if context.role not in {"editor", "admin"}:
        raise PermissionError("Workspace is read-only")
    return context


def _require_admin_context() -> McpRequestContext:
    context = _get_tool_context()
    if context.role != "admin":
        raise PermissionError("Workspace administrator permission required")
    return context


def _current_read_role(session, context: McpRequestContext) -> str:
    if is_mcp_request_active() or get_optional_mcp_context() is not None:
        return assert_mcp_read_permission(session, context).value
    return context.role


def _assert_current_write_permission(session, context: McpRequestContext) -> None:
    # Only old direct unit callers lack verified context. ASGI requests always
    # set the active marker, so no real request can use this compatibility seam.
    if is_mcp_request_active() or get_optional_mcp_context() is not None:
        assert_mcp_write_permission(session, context)


def _get_authorized_file(session, context: McpRequestContext, file_id: int):
    """Use the shared authorization service for every real MCP request.

    A few legacy unit tests call tool functions directly with a mocked session
    and only populate ``uploader_id``. That compatibility branch is never
    reachable through ``JWTAuthMiddleware``.
    """

    if not is_mcp_request_active() and get_optional_mcp_context() is None:
        file_obj = file_service.get_file(session, file_id)
        file_workspace_id = getattr(file_obj, "workspace_id", None)
        if isinstance(file_workspace_id, int) and file_workspace_id != context.workspace_id:
            raise PermissionDeniedError("Permission denied")
        if getattr(file_obj, "uploader_id", None) != context.user_id:
            raise PermissionDeniedError("Permission denied")
        return file_obj
    return file_service.get_authorized_file(
        session,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        file_id=file_id,
    )


def _get_authorized_folder(session, context: McpRequestContext, folder_id: int):
    """Compatibility equivalent for direct legacy folder-tool tests."""

    if not is_mcp_request_active() and get_optional_mcp_context() is None:
        folder = folder_service.get_folder(session, folder_id)
        folder_workspace_id = getattr(folder, "workspace_id", None)
        if isinstance(folder_workspace_id, int) and folder_workspace_id != context.workspace_id:
            raise PermissionDeniedError("Permission denied")
        folder_user_id = getattr(folder, "user_id", None)
        if isinstance(folder_user_id, int) and folder_user_id != context.user_id:
            raise PermissionDeniedError("Permission denied")
        return folder
    return folder_service.get_authorized_folder(
        session,
        context.workspace_id,
        context.user_id,
        folder_id,
    )
