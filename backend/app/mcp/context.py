"""MCP 调用的请求作用域身份上下文.

MCP 协议适配层必须同时携带经过鉴权的用户与为该请求选定的工作区。
将其保持在一个不可变的上下文中,
可防止各个工具意外将 ``user_id`` 误用作 ``workspace_id``。
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import uuid4

UNAUTHORIZED_REQUEST_MESSAGE = "Unauthorized MCP request"


class McpPermissionError(PermissionError):
    """显式的 MCP 授权拒绝异常, 携带公开安全的错误信息."""


@dataclass(frozen=True)
class McpRequestContext:
    """单个 MCP 请求的已验证身份与授权作用域模型."""

    user_id: int
    workspace_id: int
    role: str
    runtime_id: int | None = None
    actor_type: str = "user"


current_user_id: ContextVar[int | None] = ContextVar(
    "current_user_id",
    default=None,
)

_current_context: ContextVar[McpRequestContext | None] = ContextVar(
    "_current_context",
    default=None,
)
_mcp_request_active: ContextVar[bool] = ContextVar(
    "_mcp_request_active",
    default=False,
)
_current_request_id: ContextVar[str | None] = ContextVar(
    "_current_request_id",
    default=None,
)


def get_mcp_context() -> McpRequestContext:
    """返回已验证的 MCP 上下文, 或拒绝未认证的调用."""
    context = _current_context.get()
    if context is None:
        raise McpPermissionError(UNAUTHORIZED_REQUEST_MESSAGE)
    return context


def get_optional_mcp_context() -> McpRequestContext | None:
    """返回当前上下文, 若无则返回 None 而不抛出异常."""
    return _current_context.get()


def set_mcp_context(
    context: McpRequestContext | None,
) -> Token[McpRequestContext | None]:
    """设置当前请求上下文并返回重置凭据."""
    return _current_context.set(context)


def reset_mcp_context(token: Token[McpRequestContext | None]) -> None:
    """恢复在调用 ``set_mcp_context`` 之前处于激活状态的上下文."""
    _current_context.reset(token)


def set_mcp_request_active(*, active: bool) -> Token[bool]:
    """标记当前调用是否经由 ASGI MCP 网络边界进入."""
    return _mcp_request_active.set(active)


def reset_mcp_request_active(token: Token[bool]) -> None:
    """恢复之前的请求激活标记."""
    _mcp_request_active.reset(token)


def is_mcp_request_active() -> bool:
    """返回当前上下文是否属于某个活跃的 ASGI MCP 请求."""
    return _mcp_request_active.get()


def set_request_id(request_id: str | None = None) -> Token[str | None]:
    """设置审计请求 ID, 默认生成全新的 UUID."""
    return _current_request_id.set(request_id or str(uuid4()))


def reset_request_id(token: Token[str | None]) -> None:
    """恢复调用 ``set_request_id`` 之前的请求 ID."""
    _current_request_id.reset(token)


def get_request_id() -> str | None:
    """返回当前调用的审计请求 ID (若存在)."""
    return _current_request_id.get()
