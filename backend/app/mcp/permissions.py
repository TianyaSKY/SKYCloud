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
