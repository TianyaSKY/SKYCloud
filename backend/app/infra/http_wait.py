"""Shared admission wait with backend cleanup ownership and diagnostic formatting."""
from loguru import logger
from app.infra.async_stream import drain_cleanup
from runtime_controller.http_wait import guarded_wait as _guarded_wait


async def guarded_wait(operation, **kwargs):
    def report(error):
        logger.error("HTTP admission cleanup failed (error_type={})", type(error).__name__)
    return await _guarded_wait(operation, cleanup_drain=drain_cleanup, report_abort_error=report, **kwargs)
