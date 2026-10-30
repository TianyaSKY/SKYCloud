"""领域异常与 FastAPI 全局异常处理器注册.

服务层抛出 DomainError 子类; API 层统一映射为 JSON HTTP 响应。
"""

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import cast

from fastapi import FastAPI, Request
from fastapi import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from runtime_controller.json_types import JsonValue
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.contracts import (
    HTTP_ERROR_CODES,
    ErrorResponse,
    ValidationIssue,
    error_payload,
)


class DomainError(Exception):
    """预期的领域错误, 由 API 层统一映射为 HTTP 响应."""

    status_code = 400
    code = "INVALID_REQUEST"


class ResourceNotFoundError(DomainError):
    """资源不存在."""

    status_code = 404
    code = "RESOURCE_NOT_FOUND"


class BusinessRuleError(DomainError):
    """违反业务规则(参数合法但操作不允许)."""

    status_code = 400
    code = "BUSINESS_RULE_VIOLATION"


class PermissionDeniedError(DomainError):
    """无权限访问该资源."""

    status_code = 403
    code = "PERMISSION_DENIED"


class PayloadTooLargeError(DomainError):
    """请求体或上传体积超限."""

    status_code = 413
    code = "PAYLOAD_TOO_LARGE"


class ConflictError(DomainError):
    """资源冲突(如重名、状态互斥)."""

    status_code = 409
    code = "CONFLICT"


class AuthenticationError(DomainError):
    """未认证或凭证无效."""

    status_code = 401
    code = "AUTHENTICATION_REQUIRED"


class ServiceOperationError(DomainError):
    """外部资源或持久化操作失败, 且服务无法安全恢复."""

    status_code = 500
    code = "INTERNAL_ERROR"


def _to_json_safe(value: object) -> JsonValue:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, tuple):
        return [_to_json_safe(item) for item in cast("tuple[object, ...]", value)]
    if isinstance(value, list):
        return [_to_json_safe(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {str(key): _to_json_safe(item) for key, item in mapping.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return cast("JsonValue", value)


def _compact_validation_errors(
    errors: Sequence[Mapping[str, object]],
) -> list[ValidationIssue]:
    """压缩 Pydantic 校验错误, 剔除不可 JSON 序列化字段."""
    compact: list[ValidationIssue] = []
    for err in errors:
        item_type = cast("str", _to_json_safe(err.get("type", "validation_error")))
        message = {
            "missing": "Field required",
            "int_parsing": "Invalid integer",
            "string_too_short": "Value is too short",
            "string_too_long": "Value is too long",
        }.get(item_type, "Invalid value")
        # Custom validator messages/context can echo submitted credentials.
        compact.append(
            ValidationIssue(
                loc=cast("list[str | int]", _to_json_safe(err.get("loc", []))),
                type=item_type,
                msg=message,
            ),
        )
    return compact


def register_exception_handlers(app: FastAPI) -> None:
    """注册领域异常、HTTP 异常、校验失败与未捕获异常的统一响应."""
    app.router.responses.update(
        {status: {"model": ErrorResponse} for status in HTTP_ERROR_CODES},
    )

    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        message = str(exc)
        if exc.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            logger.error("服务器领域错误 (error_type={})", type(exc).__name__)
            message = "Internal server error"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.status_code, message, code=exc.code),
        )

    @app.exception_handler(StarletteHTTPException)
    @app.exception_handler(FastAPIHTTPException)
    async def handle_fastapi_http_exception(
        _: Request, exc: StarletteHTTPException,
    ) -> JSONResponse:
        detail_value = cast("object", exc.detail)
        detail = detail_value if isinstance(detail_value, str) else "Request failed"
        if exc.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            logger.error("服务器 HTTP 错误 (status={})", exc.status_code)
            detail = "Internal server error"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.status_code, detail),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        errors = exc.errors()
        msg = "Request validation failed"
        compact_errors = _compact_validation_errors(errors)
        return JSONResponse(
            status_code=422,
            content=error_payload(422, msg, issues=compact_errors),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_: Request, exc: Exception) -> JSONResponse:
        logger.error("未处理的服务器异常 (error_type={})", type(exc).__name__)
        message = "Internal server error"
        return JSONResponse(status_code=500, content=error_payload(500, message))
