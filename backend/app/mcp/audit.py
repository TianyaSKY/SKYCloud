"""MCP call audit recording without retaining tokens or file contents."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar, cast

from loguru import logger

from app.exceptions import DomainError
from app.infra.extensions import SessionLocal
from app.infra.owned_work import run_owned_persistent
from app.mcp.audit_payload import AuditArguments, summarize_call
from app.mcp.context import (
    McpPermissionError,
    get_optional_mcp_context,
    get_request_id,
    is_mcp_request_active,
)
from app.models.mcp_audit_log import McpAuditLog

_P = ParamSpec("_P")
_R = TypeVar("_R")

_SERVER_ERROR_STATUS = 500
_UNAVAILABLE_MESSAGE = "Service temporarily unavailable"


def _returned_error_type(result: object) -> str | None:
    """Recognize structured tool errors that were returned instead of raised."""
    payload: object = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    mapping = cast("dict[str, object]", payload)
    if "error" in mapping:
        return "tool_error"
    if mapping.get("errors"):
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
    """Persist one bounded audit row; failures never surface to the caller."""
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
            ),
        )
        session.commit()
    except Exception as exc:
        # Factory/SQL/driver failures must never replace a tool outcome. Preserve
        # rollback and close attempts and log only the exception's class.
        logger.error(
            "MCP audit persistence failed: error_type={}", type(exc).__name__,
        )
        if session is not None:
            try:
                session.rollback()
            except Exception as cleanup_exc:
                # Rollback can invoke driver/pool callbacks; failure cannot
                # replace the tool result and must not prevent close below.
                logger.error(
                    "MCP audit rollback failed: error_type={}",
                    type(cleanup_exc).__name__,
                )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as cleanup_exc:
                # A driver/pool close failure remains isolated from the tool,
                # including when audit persistence or rollback already failed.
                logger.error(
                    "MCP audit close failed: error_type={}",
                    type(cleanup_exc).__name__,
                )


def audited_tool(
    tool_name: str,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Decorate an async MCP tool with non-sensitive audit metadata."""

    def decorator(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        @wraps(func)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            arguments = summarize_call(func, *args, **kwargs)
            started = time.perf_counter()
            success = True
            raised = False
            error_type = None
            try:
                result = await func(*args, **kwargs)
                returned_error = _returned_error_type(result)
                if returned_error:
                    success = False
                    error_type = returned_error
            except Exception as exc:
                # FastMCP includes str(exc) in protocol errors. Keep internal
                # database/storage details out of that transport boundary.
                raised = True
                success = False
                error_type = type(exc).__name__
                if isinstance(exc, McpPermissionError) or (
                    isinstance(exc, DomainError)
                    and exc.status_code < _SERVER_ERROR_STATUS
                ):
                    raise
                logger.error(
                    "MCP tool failed: tool={} error_type={}", tool_name, error_type,
                )
                raise RuntimeError(_UNAVAILABLE_MESSAGE) from None
            except BaseException as exc:
                raised = True
                success = False
                error_type = type(exc).__name__
                raise
            else:
                return result
            finally:
                duration_ms = int((time.perf_counter() - started) * 1000)
                # Do not make a user-facing tool depend on audit persistence.
                try:
                    await run_owned_persistent(
                        record_mcp_call,
                        tool_name=tool_name,
                        arguments=arguments,
                        success=success,
                        error_type=error_type,
                        duration_ms=duration_ms,
                    )
                except asyncio.CancelledError:
                    if not raised:
                        raise

        return wrapped

    return decorator
