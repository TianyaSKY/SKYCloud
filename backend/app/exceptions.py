import logging

from fastapi import FastAPI, HTTPException as FastAPIHTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from werkzeug.exceptions import HTTPException as WerkzeugHTTPException

logger = logging.getLogger(__name__)


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
        return JSONResponse(status_code=422, content={"message": msg, "detail": msg, "errors": errors})

    @app.exception_handler(WerkzeugHTTPException)
    async def handle_werkzeug_http_exception(_: Request, exc: WerkzeugHTTPException):
        message = exc.description if isinstance(exc.description, str) else "Request failed"
        return JSONResponse(status_code=exc.code or 500, content={"message": message, "detail": message})

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_: Request, exc: Exception):
        logger.exception("Unhandled server error: %s", exc)
        message = "Internal server error"
        return JSONResponse(status_code=500, content={"message": message, "detail": message})
