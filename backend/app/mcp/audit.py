"""MCP call audit recording without retaining tokens or file contents."""

import asyncio
import json
import logging
import time
from functools import wraps
from typing import Any, Callable

from app.infra.extensions import SessionLocal
from app.mcp.context import (
    get_optional_mcp_context,
    get_request_id,
    is_mcp_request_active,
)
from app.models.mcp_audit_log import McpAuditLog

logger = logging.getLogger(__name__)


def _safe_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    return str(value)


def _summarize_arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    summary = {"args": _safe_value(args), "kwargs": _safe_value(kwargs)}
    raw = json.dumps(summary, ensure_ascii=False, default=str)
    # Keep audit data useful without recording document bodies or credentials.
    return raw[:1000]


def _first_entity_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    for value in list(args) + list(kwargs.values()):
        if isinstance(value, int):
            return value
        if isinstance(value, dict) and isinstance(value.get("id"), int):
            return value["id"]
        if isinstance(value, list):
            for item in value:
                if hasattr(item, "id") and isinstance(item.id, int):
                    return item.id
    return None


def _returned_error_type(result: Any) -> str | None:
    """Recognize structured tool errors that were returned instead of raised."""

    payload = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    if "error" in payload:
        return "tool_error"
    if payload.get("errors"):
        return "partial_tool_error"
    return None


def record_mcp_call(
        *,
        tool_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        success: bool,
        error_type: str | None,
        duration_ms: int,
) -> None:
    context = get_optional_mcp_context()
    if not context or not is_mcp_request_active():
        return
    session = SessionLocal()
    try:
        session.add(
            McpAuditLog(
                request_id=get_request_id() or "unknown",
                runtime_id=context.runtime_id,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                role=context.role,
                actor_type=context.actor_type,
                tool_name=tool_name,
                arguments_summary=_summarize_arguments(args, kwargs),
                entity_id=_first_entity_id(args, kwargs),
                success=success,
                error_type=error_type,
                duration_ms=duration_ms,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("MCP audit log failed: tool=%s", tool_name)
    finally:
        session.close()


def audited_tool(tool_name: str) -> Callable:
    """Decorate an async MCP tool with non-sensitive audit metadata."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapped(*args, **kwargs):
            started = time.perf_counter()
            success = True
            error_type = None
            try:
                result = await func(*args, **kwargs)
                returned_error = _returned_error_type(result)
                if returned_error:
                    success = False
                    error_type = returned_error
                return result
            except Exception as exc:
                success = False
                error_type = type(exc).__name__
                raise
            finally:
                duration_ms = int((time.perf_counter() - started) * 1000)
                # Do not make a user-facing tool depend on audit persistence.
                await asyncio.to_thread(
                    record_mcp_call,
                    tool_name=tool_name,
                    args=args,
                    kwargs=kwargs,
                    success=success,
                    error_type=error_type,
                    duration_ms=duration_ms,
                )

        return wrapped

    return decorator
