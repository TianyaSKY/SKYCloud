"""Authorization helpers shared by MCP tools."""

from app.mcp.context import McpRequestContext, get_mcp_context


def require_mcp_read_context() -> McpRequestContext:
    """Require an authenticated member context for read operations."""

    return get_mcp_context()


def require_mcp_write_context() -> McpRequestContext:
    """Require an editor or administrator context for write operations."""

    context = get_mcp_context()
    if context.role not in {"editor", "admin"}:
        raise PermissionError("Workspace is read-only")
    return context


def require_mcp_admin_context() -> McpRequestContext:
    """Require an administrator context for management operations."""

    context = get_mcp_context()
    if context.role != "admin":
        raise PermissionError("Workspace administrator permission required")
    return context


def assert_mcp_write_permission(session, context: McpRequestContext) -> None:
    """Revalidate database membership/role in the tool-owned transaction."""
    from app.features.workspace.permissions import assert_can_write
    assert_can_write(session, context.workspace_id, context.user_id)


def assert_mcp_read_permission(session, context: McpRequestContext):
    """Revalidate membership before any database or cache-backed read."""
    from app.features.workspace.permissions import assert_member
    return assert_member(session, context.workspace_id, context.user_id)
