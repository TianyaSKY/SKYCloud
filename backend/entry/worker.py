"""Worker 后台异步任务处理进程入口."""

import threading

from app import initialize_application
from app.infra.logging_setup import setup_logging
from app.infra.worker_runtime import run_scheduler, run_worker


def main() -> None:
    """初始化应用程序, 启动定时调度器并消费处理后台任务."""
    setup_logging()
    initialize_application()

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    run_worker()


if __name__ == "__main__":
    main()
