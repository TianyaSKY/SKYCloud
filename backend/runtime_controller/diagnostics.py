"""Private failure diagnostics for the standalone Controller process."""

import copy
import logging
from collections.abc import Iterable
from typing import Final, TextIO

HTTP_METHODS: Final = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "CONNECT", "TRACE"},
)
MIN_STATUS_CODE: Final = 100
MAX_STATUS_CODE: Final = 599
ACCESS_LOG_FIELDS: Final = 5
ACCESS_STATUS_INDEX: Final = 4
ACCESS_METHOD_INDEX: Final = 1
LIBRARY_FAILURE_MESSAGE: Final = "Library failure in %s (error_type=%s)"
ACCESS_MESSAGE: Final = "Controller HTTP request (method=%s status=%s)"
LOG_FORMAT: Final = "%(levelname)s %(name)s %(message)s"
LIBRARY_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


def _access_fields(record: logging.LogRecord) -> tuple[object, ...]:
    """Return the uvicorn access tuple, or an empty tuple for other records."""
    return record.args if isinstance(record.args, tuple) else ()


def _access_method(fields: tuple[object, ...]) -> str:
    candidate = fields[ACCESS_METHOD_INDEX]
    if isinstance(candidate, str) and candidate in HTTP_METHODS:
        return candidate
    return "unknown"


def _access_status(fields: tuple[object, ...]) -> int | str:
    candidate = fields[ACCESS_STATUS_INDEX]
    if type(candidate) is int and MIN_STATUS_CODE <= candidate <= MAX_STATUS_CODE:
        return candidate
    return "unknown"


class PrivateDiagnosticsHandler(logging.StreamHandler[TextIO]):
    """Discard library exception text before formatting, including Uvicorn rethrows."""

    def emit(self, record: logging.LogRecord) -> None:
        """Sanitize the record in place on a copy, then emit it."""
        error = record.exc_info[1] if record.exc_info is not None else None
        if error is not None:
            record = copy.copy(record)
            record.msg = LIBRARY_FAILURE_MESSAGE
            record.args = (record.name, type(error).__name__)
            record.exc_info = record.exc_text = record.stack_info = None
        elif record.name == "uvicorn.access":
            record = copy.copy(record)
            fields = _access_fields(record)
            method = "unknown"
            status: int | str = "unknown"
            if len(fields) == ACCESS_LOG_FIELDS:
                method = _access_method(fields)
                status = _access_status(fields)
            record.msg = ACCESS_MESSAGE
            record.args = (method, status)
            record.exc_text = record.stack_info = None
        super().emit(record)


def _library_loggers() -> Iterable[logging.Logger]:
    return (logging.getLogger(name) for name in LIBRARY_LOGGERS)


def setup_logging() -> None:
    """Install the sanitizing handler for the process and library loggers."""
    handler = PrivateDiagnosticsHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    # HTTP client INFO records contain full runtime relay URLs and query strings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for logger in _library_loggers():
        logger.handlers = [handler]
        logger.propagate = False
