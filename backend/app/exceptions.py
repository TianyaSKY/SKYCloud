import logging
from typing import Any

from fastapi import FastAPI, HTTPException as FastAPIHTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from werkzeug.exceptions import HTTPException as WerkzeugHTTPException

logger = logging.getLogger(__name__)


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

    @app.exception_handler(WerkzeugHTTPException)
    async def handle_werkzeug_http_exception(_: Request, exc: WerkzeugHTTPException):
        message = exc.description if isinstance(exc.description, str) else "Request failed"
        return JSONResponse(status_code=exc.code or 500, content={"message": message, "detail": message})

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_: Request, exc: Exception):
        logger.exception("Unhandled server error: %s", exc)
        message = "Internal server error"
        return JSONResponse(status_code=500, content={"message": message, "detail": message})
