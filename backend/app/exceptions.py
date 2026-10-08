"""领域异常与 FastAPI 全局异常处理器注册。

服务层抛出 DomainError 子类；API 层统一映射为 JSON HTTP 响应。
"""

from typing import Any

from fastapi import FastAPI, HTTPException as FastAPIHTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.api.contracts import ErrorResponse, HTTP_ERROR_CODES, error_payload


class DomainError(Exception):
    """预期的领域错误，由 API 层统一映射为 HTTP 响应。"""

    status_code = 400
    code = "INVALID_REQUEST"


class ResourceNotFoundError(DomainError):
    """资源不存在。"""

    status_code = 404
    code = "RESOURCE_NOT_FOUND"


class BusinessRuleError(DomainError):
    """违反业务规则（参数合法但操作不允许）。"""

    status_code = 400
    code = "BUSINESS_RULE_VIOLATION"


class PermissionDeniedError(DomainError):
    """无权限访问该资源。"""

    status_code = 403
    code = "PERMISSION_DENIED"


class PayloadTooLargeError(DomainError):
    """请求体或上传体积超限。"""

    status_code = 413
    code = "PAYLOAD_TOO_LARGE"


class ConflictError(DomainError):
    """资源冲突（如重名、状态互斥）。"""

    status_code = 409
    code = "CONFLICT"


class AuthenticationError(DomainError):
    """未认证或凭证无效。"""

    status_code = 401
    code = "AUTHENTICATION_REQUIRED"


class ServiceOperationError(DomainError):
    """外部资源或持久化操作失败，且服务无法安全恢复。"""

    status_code = 500
    code = "INTERNAL_ERROR"


def _to_json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, tuple):
        return [_to_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_to_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    return value


def _compact_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """压缩 Pydantic 校验错误，剔除不可 JSON 序列化字段。"""
    compact: list[dict[str, Any]] = []
    for err in errors:
        item = {
            "loc": _to_json_safe(err.get("loc", [])),
            "msg": _to_json_safe(err.get("msg", "Request validation failed")),
            "type": _to_json_safe(err.get("type", "validation_error")),
        }
        # Custom validator messages/context can echo submitted credentials.
        item["msg"] = {"missing":"Field required", "int_parsing":"Invalid integer",
                       "string_too_short":"Value is too short", "string_too_long":"Value is too long"}.get(item["type"], "Invalid value")
        compact.append(item)
    return compact


def register_exception_handlers(app: FastAPI) -> None:
    """注册领域异常、HTTP 异常、校验失败与未捕获异常的统一响应。"""

    app.router.responses.update({status: {"model": ErrorResponse} for status in HTTP_ERROR_CODES})

    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError):
        message = str(exc)
        if exc.status_code >= 500:
            logger.error("服务器领域错误 (error_type={})", type(exc).__name__)
            message = "Internal server error"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.status_code, message, code=exc.code),
        )

    @app.exception_handler(StarletteHTTPException)
    @app.exception_handler(FastAPIHTTPException)
    async def handle_fastapi_http_exception(_: Request, exc: FastAPIHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        if exc.status_code >= 500:
            logger.error("服务器 HTTP 错误 (status={})", exc.status_code)
            detail = "Internal server error"
        return JSONResponse(status_code=exc.status_code, content=error_payload(exc.status_code, detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        msg = "Request validation failed"
        compact_errors = _compact_validation_errors(errors)
        return JSONResponse(
            status_code=422,
            content=error_payload(422,msg,issues=compact_errors),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_: Request, exc: Exception):
        logger.error("未处理的服务器异常 (error_type={})", type(exc).__name__)
        message = "Internal server error"
        return JSONResponse(status_code=500, content=error_payload(500,message))
