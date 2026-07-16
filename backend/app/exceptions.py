from typing import Any

from fastapi import FastAPI, HTTPException as FastAPIHTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger


class DomainError(Exception):
    """预期的领域错误，由 API 层统一映射为 HTTP 响应。"""

    status_code = 400


class ResourceNotFoundError(DomainError):
    status_code = 404


class BusinessRuleError(DomainError):
    status_code = 400


class PermissionDeniedError(DomainError):
    status_code = 403


class PayloadTooLargeError(DomainError):
    status_code = 413


class ConflictError(DomainError):
    status_code = 409


class AuthenticationError(DomainError):
    status_code = 401


class ServiceOperationError(DomainError):
    """外部资源或持久化操作失败，且服务无法安全恢复。"""

    status_code = 500


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
    compact: list[dict[str, Any]] = []
    for err in errors:
        item = {
            "loc": _to_json_safe(err.get("loc", [])),
            "msg": _to_json_safe(err.get("msg", "Request validation failed")),
            "type": _to_json_safe(err.get("type", "validation_error")),
        }
        ctx = err.get("ctx")
        if ctx:
            item["ctx"] = _to_json_safe(ctx)
        compact.append(item)
    return compact


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": str(exc), "detail": str(exc)},
        )

    @app.exception_handler(FastAPIHTTPException)
    async def handle_fastapi_http_exception(_: Request, exc: FastAPIHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(status_code=exc.status_code, content={"message": detail, "detail": detail})

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        first_error = errors[0] if errors else {}
        msg = first_error.get("msg", "Request validation failed")
        compact_errors = _compact_validation_errors(errors)
        return JSONResponse(
            status_code=422,
            content={"message": msg, "detail": msg, "errors": compact_errors},
        )



    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_: Request, exc: Exception):
        logger.exception("未处理的服务器异常：{}", exc)
        message = "Internal server error"
        return JSONResponse(status_code=500, content={"message": message, "detail": message})
