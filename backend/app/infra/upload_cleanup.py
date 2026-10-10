"""Best-effort partial-file removal without replacing the upload failure."""
import os
from loguru import logger


def remove_partial_safely(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        # The owning staging/reservation cleanup can retry removal later.
        # Paths and operating-system error messages may contain private data.
        logger.warning('Partial upload cleanup deferred (error_type={})', type(exc).__name__)
