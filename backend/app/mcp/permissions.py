"""MCP 工具共享的鉴权辅助函数集合."""

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
    """读操作要求当前上下文为已认证的工作区成员."""
    return get_mcp_context()


def require_mcp_write_context() -> McpRequestContext:
    """写操作要求当前上下文具备 editor 或 administrator 角色."""
    context = get_mcp_context()
    if context.role not in {"editor", "admin"}:
        raise McpPermissionError(READ_ONLY_MESSAGE)
    return context


def require_mcp_admin_context() -> McpRequestContext:
    """管理操作要求当前上下文具备 administrator 管理员角色."""
    context = get_mcp_context()
    if context.role != "admin":
        raise McpPermissionError(ADMIN_REQUIRED_MESSAGE)
    return context


def assert_mcp_write_permission(session: Session, context: McpRequestContext) -> None:
    """在工具拥有的数据库事务中重新验证成员资格与角色."""
    assert_can_write_locked(session, context.workspace_id, context.user_id)


def assert_mcp_read_permission(
    session: Session,
    context: McpRequestContext,
) -> WorkspaceRole:
    """在任何数据库或缓存读取之前重新验证成员资格."""
    return assert_member(session, context.workspace_id, context.user_id)
