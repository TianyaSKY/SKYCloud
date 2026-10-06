"""Synchronous RabbitMQ worker runtime.

The API process is async, but this process is intentionally synchronous: each
task is a normal call chain executed by a thread.  The runtime owns executor
capacity and futures; business handlers do not know about semaphores or
RabbitMQ.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from app.features.file.service import cleanup_expired_uploads
from app.infra.extensions import SessionLocal
from app.features.folder import service as folder_service
from app.features.folder.organize.handler import handle_organize_process
from app.infra.indexing.handler import handle_file_indexing as handle_file_process
from app.infra.llm.sync_client import close_sync_clients
from app.infra.file_task_outbox import deliver_pending_file_tasks
from app.infra.file_object_deletion import delete_pending_objects
from app.infra.runtime_container_deletion import delete_pending_containers
from app.infra.runtime_operation_cleanup import retire_cancelled_operations
from app.infra.runtime_operations import recover_runtime_operations
from app.infra.organize_tasks import task_execution, deliver_pending_organize_tasks, RetryTaskLater
from sqlalchemy.exc import SQLAlchemyError
from app.infra.task_queue import (
    FILE_PROCESS_QUEUE,
    ORGANIZE_FILE_QUEUE,
    RABBITMQ_RECONNECT_DELAY_SECONDS,
    QueueMessage,
    RabbitMQTaskConsumer,
)

from loguru import logger
from pika.exceptions import AMQPError

ORGANIZE_WORKERS = max(1, int(os.getenv("WORKER_ORGANIZE_THREADS", "1")))


def run_scheduler() -> None:
    """Run periodic upload cleanup in a daemon thread."""
    logger.info("Scheduler thread started")
    next_cleanup = 0.0
    while True:
        try:
            deliver_pending_file_tasks()
        except Exception:
            logger.exception("Error delivering pending file tasks")
        try:
            deliver_pending_organize_tasks()
        except Exception:
            logger.exception("Error delivering pending organize tasks")
        try:
            delete_pending_objects()
        except Exception:
            logger.exception("Error cleaning pending file objects")
        try:
            delete_pending_containers()
        except Exception:
            logger.exception("Error cleaning pending runtime containers")
        try:
            retire_cancelled_operations()
        except Exception:
            logger.exception("Error retiring cancelled runtime operations")
        try:
            recover_runtime_operations()
        except Exception:
            logger.exception("Error recovering runtime operations")
        if time.monotonic() >= next_cleanup:
            try:
                with SessionLocal() as session:
                    cleanup_expired_uploads(max_age_hours=24, session=session)
            except Exception:
                logger.exception("Error in upload cleanup")
            next_cleanup = time.monotonic() + 60
        time.sleep(1)


def process_task(file_id: int) -> None:
    """Process one file; executor capacity is owned by :class:`WorkerRuntime`."""
    handle_file_process(file_id)


def process_organize_task(
    workspace_id: int,
    user_id: int,
    lock_token: str | None = None,
) -> None:
    """Run under a database lease; terminal replay never calls the model again."""
    try:
        with task_execution(workspace_id, user_id, lock_token, session_factory=SessionLocal) as claim:
            if claim is None:
                return
            handle_organize_process(workspace_id, user_id, task_id=lock_token, execution_token=claim)
    except SQLAlchemyError as error:
        raise RetryTaskLater("Organize database operation failed") from error


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
                logger.exception("Worker task failed in {} executor", queue_name)

    def reap_completed(self) -> None:
        self._reap("index")
        self._reap("organize")

    def available_queues(self) -> list[str]:
        """Only fetch work that can start without blocking the connection thread."""
        self.reap_completed()
        return [
            queue for queue, pool, capacity in (
                (FILE_PROCESS_QUEUE, "index", self.index_workers),
                (ORGANIZE_FILE_QUEUE, "organize", self.organize_workers),
            ) if len(self._running[pool]) < capacity
        ]

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
                    logger.exception("Worker task failed in {} executor", queue_name)

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
) -> Future[object]:
    """Decode one queue message and submit it to its dedicated executor."""
    if message.queue_name == FILE_PROCESS_QUEUE:
        return runtime.submit_index(process_task, int(message.body))

    if message.queue_name == ORGANIZE_FILE_QUEUE:
        workspace_id, user_id, lock_token = parse_organize_message(message.body)
        return runtime.submit_organize(
            process_organize_task,
            workspace_id,
            user_id,
            lock_token,
        )

    raise ValueError(f"Unknown task queue: {message.queue_name}")


def worker_step(
    runtime: WorkerRuntime,
    consumer: RabbitMQTaskConsumer,
    inflight: dict[Future[object], QueueMessage],
) -> None:
    """Settle deliveries and fetch only available work, all on the connection thread."""
    for future, message in list(inflight.items()):
        if not future.done():
            continue
        error = future.exception()
        if isinstance(error, RetryTaskLater):
            consumer.retry(message)
            del inflight[future]
            consumer.idle()
            return
        if error is None:
            consumer.acknowledge(message)
        else:
            consumer.park_failed(message, error)
        del inflight[future]
    message = consumer.poll(runtime.available_queues())
    if message is None:
        consumer.idle()
        return
    try:
        if message.raw_body is not None:
            message.raw_body.decode("utf-8")
        future = dispatch_message(runtime, message)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        consumer.park_failed(message, error)
    else:
        inflight[future] = message


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
    inflight: dict[Future[object], QueueMessage] = {}
    try:
        while True:
            try:
                worker_step(runtime, consumer, inflight)
            except AMQPError:
                consumer.close()
                logger.exception("RabbitMQ delivery failed; reconnecting")
                time.sleep(RABBITMQ_RECONNECT_DELAY_SECONDS)
    finally:
        consumer.close()
        runtime.shutdown()
