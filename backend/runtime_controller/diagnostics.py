"""独立 Controller 进程的内部失败诊断日志处理."""

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
    """返回 uvicorn 访问元组, 若非访问记录则返回空元组."""
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
    """在格式化前过滤丢弃底层库异常文本, 包括 Uvicorn 重抛的异常."""

    def emit(self, record: logging.LogRecord) -> None:
        """在副本上就地脱敏日志记录, 随后将其输出."""
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
    """为当前进程及第三方库日志记录器安装脱敏日志处理器."""
    handler = PrivateDiagnosticsHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    # HTTP 客户端的 INFO 日志包含完整的运行时中继 URL 和查询字符串.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for logger in _library_loggers():
        logger.handlers = [handler]
        logger.propagate = False
