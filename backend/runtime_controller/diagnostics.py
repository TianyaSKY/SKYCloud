"""Private failure diagnostics for the standalone Controller process."""
import copy
import logging


class PrivateDiagnosticsHandler(logging.StreamHandler):
    """Discard library exception text before formatting, including Uvicorn rethrows."""

    def emit(self, record):
        if record.exc_info and record.exc_info[1] is not None:
            record = copy.copy(record)
            record.msg = 'Library failure in %s (error_type=%s)'
            record.args = (record.name, type(record.exc_info[1]).__name__)
            record.exc_info = record.exc_text = record.stack_info = None
        elif record.name == 'uvicorn.access':
            record = copy.copy(record)
            fields = record.args if isinstance(record.args, tuple) else ()
            method = 'unknown'
            status: int | str = 'unknown'
            if len(fields) == 5:
                if fields[1] in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD', 'CONNECT', 'TRACE'):
                    method = fields[1]
                if type(fields[4]) is int and 100 <= fields[4] <= 599:
                    status = fields[4]
            record.msg = 'Controller HTTP request (method=%s status=%s)'
            record.args = (method, status)
            record.exc_text = record.stack_info = None
        super().emit(record)


def setup_logging():
    handler = PrivateDiagnosticsHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s %(name)s %(message)s'))
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    # HTTP client INFO records contain full runtime relay URLs and query strings.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'uvicorn.asgi'):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
