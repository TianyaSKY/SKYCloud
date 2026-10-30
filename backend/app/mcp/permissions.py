"""Authorization helpers shared by MCP tools."""

from sqlalchemy.orm import Session

from app.features.workspace.permissions import (
    WorkspaceRole,
    assert_can_write_locked,
    assert_member,
)
from app.mcp.context import McpPermissionError, McpRequestContext, get_mcp_context

READ_ONLY_MESSAGE = "Workspace is read-only"
ADMIN_REQUIRED_MESSAGE = "Workspace administrator permission required"


def require_mcp_read_context() -> McpRequestContext:
    """Require an authenticated member context for read operations."""
    return get_mcp_context()


def require_mcp_write_context() -> McpRequestContext:
    """Require an editor or administrator context for write operations."""
    context = get_mcp_context()
    if context.role not in {"editor", "admin"}:
        raise McpPermissionError(READ_ONLY_MESSAGE)
    return context


def require_mcp_admin_context() -> McpRequestContext:
    """Require an administrator context for management operations."""
    context = get_mcp_context()
    if context.role != "admin":
        raise McpPermissionError(ADMIN_REQUIRED_MESSAGE)
    return context


def assert_mcp_write_permission(session: Session, context: McpRequestContext) -> None:
    """Revalidate database membership/role in the tool-owned transaction."""
    assert_can_write_locked(session, context.workspace_id, context.user_id)


def assert_mcp_read_permission(
    session: Session,
    context: McpRequestContext,
) -> WorkspaceRole:
    """Revalidate membership before any database or cache-backed read."""
    return assert_member(session, context.workspace_id, context.user_id)
