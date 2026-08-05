"""Worker process entry point."""

import logging
import threading

from app import initialize_application
from app.infra.worker_runtime import run_scheduler, run_worker


def main() -> None:
    """Initialize the application, start maintenance, and consume tasks."""
    logging.basicConfig(level=logging.INFO)
    initialize_application()

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    run_worker()


if __name__ == "__main__":
    main()
