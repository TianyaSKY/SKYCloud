"""Request-scoped identity for MCP calls.

The MCP protocol adapter must carry both the authenticated user and the
workspace selected for the request.  Keeping this in one immutable context
prevents individual tools from accidentally treating ``user_id`` as a
``workspace_id``.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class McpRequestContext:
    """Verified identity and authorization scope for one MCP request."""

    user_id: int
    workspace_id: int
    role: str
    runtime_id: int | None = None
    actor_type: str = "user"


_current_context: ContextVar[McpRequestContext | None] = ContextVar(
    "_current_context", default=None
)
_mcp_request_active: ContextVar[bool] = ContextVar(
    "_mcp_request_active", default=False
)
_current_request_id: ContextVar[str | None] = ContextVar(
    "_current_request_id", default=None
)


def get_mcp_context() -> McpRequestContext:
    """Return the verified MCP context or reject an unauthenticated call."""

    context = _current_context.get()
    if context is None:
        raise PermissionError("Unauthorized MCP request")
    return context


def get_optional_mcp_context() -> McpRequestContext | None:
    """Return the current context without raising."""

    return _current_context.get()


def set_mcp_context(context: McpRequestContext | None) -> Token:
    """Set the current request context and return a reset token."""

    return _current_context.set(context)


def reset_mcp_context(token: Token) -> None:
    """Restore the context that was active before ``set_mcp_context``."""

    _current_context.reset(token)


def set_mcp_request_active(active: bool) -> Token:
    """Mark whether the current call came through the ASGI MCP boundary."""

    return _mcp_request_active.set(active)


def reset_mcp_request_active(token: Token) -> None:
    _mcp_request_active.reset(token)


def is_mcp_request_active() -> bool:
    return _mcp_request_active.get()


def set_request_id(request_id: str | None = None) -> Token:
    return _current_request_id.set(request_id or str(uuid4()))


def reset_request_id(token: Token) -> None:
    _current_request_id.reset(token)


def get_request_id() -> str | None:
    return _current_request_id.get()
