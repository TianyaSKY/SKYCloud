"""记录 MCP 调用审计日志, 绝不持久化存储令牌或文件内容."""

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
    """识别返回结构化工具错误而非抛出异常的情况."""
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
    """持久化写入单条有界审计记录; 失败绝不向调用方抛出异常."""
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
        # 工厂/SQL/驱动层失败绝不能取代工具本身的结果。保留
        # 回滚与关闭尝试, 且仅记录异常的类型名称。
        logger.error(
            "MCP audit persistence failed: error_type={}", type(exc).__name__,
        )
        if session is not None:
            try:
                session.rollback()
            except Exception as cleanup_exc:
                # 回滚可能触发驱动/连接池回调; 失败绝不能
                # 覆盖工具结果, 且不得阻碍下方的关闭逻辑。
                logger.error(
                    "MCP audit rollback failed: error_type={}",
                    type(cleanup_exc).__name__,
                )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as cleanup_exc:
                # 驱动/连接池关闭失败始终与工具结果相隔离,
                # 包括在审计持久化或回滚已经失败的情况下。
                logger.error(
                    "MCP audit close failed: error_type={}",
                    type(cleanup_exc).__name__,
                )


def audited_tool(
    tool_name: str,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """为异步 MCP 工具添加不含敏感信息的审计装饰器."""

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
                # FastMCP 会将 str(exc) 包含在协议错误中。保持内部
                # 数据库/存储细节不泄露到该传输层边界之外。
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
                # 不让面向用户的工具结果依赖于审计持久化的成败。
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
