"""MCP call audit recording without retaining tokens or file contents."""

import asyncio
import json
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
from app.mcp.audit_payload import AuditArguments, summarize_call

from loguru import logger


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
        arguments: AuditArguments,
        success: bool,
        error_type: str | None,
        duration_ms: int,
) -> None:
    context = get_optional_mcp_context()
    if not context or not is_mcp_request_active():
        return
    session = None
    try:
        session = SessionLocal()
        session.add(
            McpAuditLog(
                request_id=get_request_id() or "unknown",
                runtime_id=context.runtime_id,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                role=context.role,
                actor_type=context.actor_type,
                tool_name=tool_name,
                arguments_summary=arguments.summary,
                entity_id=arguments.entity_id,
                success=success,
                error_type=error_type,
                duration_ms=duration_ms,
            )
        )
        session.commit()
    except Exception as exc:
        logger.error("MCP audit persistence failed: error_type={}", type(exc).__name__)
        if session is not None:
            try:
                session.rollback()
            except Exception as cleanup_exc:
                logger.error("MCP audit rollback failed: error_type={}", type(cleanup_exc).__name__)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as cleanup_exc:
                logger.error("MCP audit close failed: error_type={}", type(cleanup_exc).__name__)


def audited_tool(tool_name: str) -> Callable:
    """Decorate an async MCP tool with non-sensitive audit metadata."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapped(*args, **kwargs):
            arguments = summarize_call(func, args, kwargs)
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
            except BaseException as exc:
                success = False
                error_type = type(exc).__name__
                raise
            finally:
                duration_ms = int((time.perf_counter() - started) * 1000)
                # Do not make a user-facing tool depend on audit persistence.
                await asyncio.to_thread(
                    record_mcp_call,
                    tool_name=tool_name,
                    arguments=arguments,
                    success=success,
                    error_type=error_type,
                    duration_ms=duration_ms,
                )

        return wrapped

    return decorator
