"""Synchronous RabbitMQ worker runtime.

The API process is async, but this process is intentionally synchronous: each
task is a normal call chain executed by a thread.  The runtime owns executor
capacity and futures; business handlers do not know about semaphores or
RabbitMQ.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from app.features.file.service import cleanup_expired_uploads
from app.features.folder import service as folder_service
from app.features.folder.organize.handler import handle_organize_process
from app.infra.indexing.handler import handle_file_indexing as handle_file_process
from app.infra.llm.sync_client import close_sync_clients
from app.infra.task_queue import (
    FILE_PROCESS_QUEUE,
    ORGANIZE_FILE_QUEUE,
    QueueMessage,
    RabbitMQTaskConsumer,
)

logger = logging.getLogger(__name__)

ORGANIZE_WORKERS = max(1, int(os.getenv("WORKER_ORGANIZE_THREADS", "1")))


def run_scheduler() -> None:
    """Run periodic upload cleanup in a daemon thread."""
    logger.info("Scheduler thread started")
    while True:
        try:
            cleanup_expired_uploads(max_age_hours=24)
        except Exception:
            logger.exception("Error in scheduler")
        time.sleep(3600)


def process_task(file_id: int) -> None:
    """Process one file; executor capacity is owned by :class:`WorkerRuntime`."""
    handle_file_process(file_id)


def process_organize_task(
    workspace_id: int,
    user_id: int,
    lock_token: str | None = None,
) -> None:
    """Run one long-lived organize task and always release its distributed lock."""
    token = lock_token or f"legacy-{workspace_id}"
    try:
        folder_service.mark_organize_task_running(workspace_id, token)
        logger.info("Starting organize_files for workspace %s", workspace_id)
        result = handle_organize_process(workspace_id, user_id)
        logger.info("Finished organize_files for workspace %s: %s", workspace_id, result)
    except Exception:
        logger.exception("Error organizing files for workspace %s", workspace_id)
    finally:
        folder_service.release_organize_task_lock(workspace_id, token)


class WorkerRuntime:
    """Own thread pools and in-flight futures for the worker process."""

    def __init__(self, index_workers: int, organize_workers: int = ORGANIZE_WORKERS):
        self.index_workers = max(1, index_workers)
        self.organize_workers = max(1, organize_workers)
        self.index_executor = ThreadPoolExecutor(
            max_workers=self.index_workers,
            thread_name_prefix="index-worker",
        )
        self.organize_executor = ThreadPoolExecutor(
            max_workers=self.organize_workers,
            thread_name_prefix="organize-worker",
        )
        self._running: dict[str, set[Future[object]]] = {
            "index": set(),
            "organize": set(),
        }

    def _reap(self, queue_name: str) -> None:
        running = self._running[queue_name]
        done = {future for future in running if future.done()}
        running.difference_update(done)
        for future in done:
            try:
                future.result()
            except Exception:
                logger.exception("Worker task failed in %s executor", queue_name)

    def reap_completed(self) -> None:
        self._reap("index")
        self._reap("organize")

    def wait_for_capacity(self, queue_name: str) -> None:
        """Block only at the runtime boundary until the selected pool has room."""
        capacity = self.index_workers if queue_name == "index" else self.organize_workers
        running = self._running[queue_name]
        while len(running) >= capacity:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            running.difference_update(done)
            for future in done:
                try:
                    future.result()
                except Exception:
                    logger.exception("Worker task failed in %s executor", queue_name)

    def submit_index(self, func, *args: object) -> Future[object]:
        self.wait_for_capacity("index")
        future = self.index_executor.submit(func, *args)
        self._running["index"].add(future)
        return future

    def submit_organize(self, func, *args: object) -> Future[object]:
        self.wait_for_capacity("organize")
        future = self.organize_executor.submit(func, *args)
        self._running["organize"].add(future)
        return future

    def shutdown(self) -> None:
        """Stop executors and release the sync LLM connection pool."""
        self.index_executor.shutdown(wait=True)
        self.organize_executor.shutdown(wait=True)
        close_sync_clients()


def parse_organize_message(data: str) -> tuple[int, int, str | None]:
    """Parse current JSON payloads and the legacy workspace-only payload."""
    try:
        payload = json.loads(data)
        if isinstance(payload, dict):
            return (
                int(payload["workspace_id"]),
                int(payload.get("user_id") or 0),
                str(payload.get("lock_token") or "") or None,
            )
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        pass

    return int(data), 0, None


def dispatch_message(
    runtime: WorkerRuntime,
    message: QueueMessage,
) -> None:
    """Decode one queue message and submit it to its dedicated executor."""
    if message.queue_name == FILE_PROCESS_QUEUE:
        runtime.wait_for_capacity("index")
        runtime.submit_index(process_task, int(message.body))
        return

    if message.queue_name == ORGANIZE_FILE_QUEUE:
        runtime.wait_for_capacity("organize")
        workspace_id, user_id, lock_token = parse_organize_message(message.body)
        runtime.submit_organize(
            process_organize_task,
            workspace_id,
            user_id,
            lock_token,
        )
        return

    raise ValueError(f"Unknown task queue: {message.queue_name}")


def run_worker(max_workers: int | None = None) -> None:
    """Consume tasks forever using independent index and organize pools."""
    index_workers = max_workers or max(
        1, int(os.getenv("WORKER_MAX_THREADS", "5"))
    )
    logger.info(
        "Worker started with index_workers=%s, organize_workers=%s",
        index_workers,
        ORGANIZE_WORKERS,
    )

    consumer = RabbitMQTaskConsumer()
    runtime = WorkerRuntime(index_workers=index_workers)
    try:
        while True:
            runtime.reap_completed()
            message = consumer.get_next_message()
            try:
                dispatch_message(runtime, message)
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.exception(
                    "Invalid data received from queue %s: %s",
                    message.queue_name,
                    message.body,
                )
    finally:
        consumer.close()
        runtime.shutdown()
