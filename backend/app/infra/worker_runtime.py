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
from typing import TYPE_CHECKING

from loguru import logger
from pika.exceptions import AMQPError
from sqlalchemy.exc import SQLAlchemyError

from app.features.file.service import cleanup_expired_uploads
from app.features.file.tasks import deliver_pending_file_tasks
from app.features.folder.organize.handler import handle_organize_process
from app.features.folder.organize.tasks import (
    RetryTaskLater,
    deliver_pending_organize_tasks,
    task_execution,
)
from app.infra.assistant_abort import abort_pending_sessions, recover_abandoned_runs
from app.infra.extensions import SessionLocal
from app.infra.file_object_deletion import delete_pending_objects
from app.infra.indexing.handler import handle_file_indexing as handle_file_process
from app.infra.llm.sync_client import close_sync_clients
from app.infra.runtime_container_deletion import delete_pending_containers
from app.infra.runtime_operation_cleanup import retire_cancelled_operations
from app.infra.runtime_operations import recover_runtime_operations
from app.infra.task_queue import (
    FILE_PROCESS_QUEUE,
    ORGANIZE_FILE_QUEUE,
    RABBITMQ_RECONNECT_DELAY_SECONDS,
    RabbitMQTaskConsumer,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from runtime_common.json_types import JsonValue

    from app.infra.task_queue import QueueMessage

ORGANIZE_WORKERS = max(1, int(os.getenv("WORKER_ORGANIZE_THREADS", "1")))


def _run_isolated(label: str, action: Callable[[], int | None]) -> None:
    """Run one maintenance action without stopping unrelated scheduling."""
    try:
        action()
    except Exception as exc:
        # Each maintenance action crosses arbitrary SDK/business failure paths;
        # isolate the whole subtask so later actions and ticks still run.
        logger.error("Error {} (error_type={})", label, type(exc).__name__)


def _recover_assistant_sessions() -> None:
    """Recover runs before retrying their remote session cleanup."""
    recover_abandoned_runs()
    abort_pending_sessions()


def _cleanup_uploads() -> int:
    """Clean expired uploads within the existing short-lived SQL session."""
    with SessionLocal() as session:
        return cleanup_expired_uploads(max_age_hours=24, session=session)


def run_scheduler() -> None:
    """Run periodic upload cleanup in a daemon thread."""
    logger.info("Scheduler thread started")
    next_cleanup = 0.0
    while True:
        _run_isolated("delivering pending file tasks", deliver_pending_file_tasks)
        _run_isolated(
            "delivering pending organize tasks",
            deliver_pending_organize_tasks,
        )
        _run_isolated("cleaning pending file objects", delete_pending_objects)
        _run_isolated("cleaning pending runtime containers", delete_pending_containers)
        _run_isolated(
            "retiring cancelled runtime operations",
            retire_cancelled_operations,
        )
        _run_isolated("recovering runtime operations", recover_runtime_operations)
        _run_isolated("recovering assistant sessions", _recover_assistant_sessions)
        if time.monotonic() >= next_cleanup:
            _run_isolated("in upload cleanup", _cleanup_uploads)
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
        with task_execution(
            workspace_id,
            user_id,
            lock_token,
            session_factory=SessionLocal,
        ) as claim:
            if claim is None:
                return
            handle_organize_process(
                workspace_id,
                user_id,
                task_id=lock_token,
                execution_token=claim,
            )
    except SQLAlchemyError as error:
        message = "Organize database operation failed"
        raise RetryTaskLater(message) from error


def _report_task_failures(
    queue_name: str,
    futures: Iterable[Future[None]],
) -> None:
    """Consume each completed result without letting a failed handler escape."""
    for future in futures:
        try:
            future.result()
        except Exception as exc:
            # Arbitrary task handlers may raise any application/SDK exception;
            # one completed task must never prevent reaping the other futures.
            logger.error(
                "Worker task failed in {} executor (error_type={})",
                queue_name,
                type(exc).__name__,
            )


class WorkerRuntime:
    """Own thread pools and in-flight futures for the worker process."""

    def __init__(
        self,
        index_workers: int,
        organize_workers: int = ORGANIZE_WORKERS,
    ) -> None:
        """Create independent bounded pools for indexing and organizing."""
        self.index_workers = max(1, index_workers)
        self.organize_workers = max(1, organize_workers)
        self.index_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=self.index_workers,
            thread_name_prefix="index-worker",
        )
        self.organize_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=self.organize_workers,
            thread_name_prefix="organize-worker",
        )
        self._running: dict[str, set[Future[None]]] = {
            "index": set(),
            "organize": set(),
        }

    def _reap(self, queue_name: str) -> None:
        running = self._running[queue_name]
        done = {future for future in running if future.done()}
        running.difference_update(done)
        _report_task_failures(queue_name, done)

    def reap_completed(self) -> None:
        """Reap finished tasks in both pools without propagating their failures."""
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
        capacity = (
            self.index_workers if queue_name == "index" else self.organize_workers
        )
        running = self._running[queue_name]
        while len(running) >= capacity:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            running.difference_update(done)
            _report_task_failures(queue_name, done)

    def _submit(
        self,
        queue_name: str,
        executor: ThreadPoolExecutor,
        func: Callable[..., None],
        *args: object,
    ) -> Future[None]:
        """Wait for capacity before tracking the newly submitted task."""
        self.wait_for_capacity(queue_name)
        future = executor.submit(func, *args)
        self._running[queue_name].add(future)
        return future

    def submit_index(
        self,
        func: Callable[..., None],
        *args: object,
    ) -> Future[None]:
        """Submit an indexing task only when its pool has capacity."""
        return self._submit("index", self.index_executor, func, *args)

    def submit_organize(
        self,
        func: Callable[..., None],
        *args: object,
    ) -> Future[None]:
        """Submit an organizer task only when its pool has capacity."""
        return self._submit("organize", self.organize_executor, func, *args)

    def shutdown(self) -> None:
        """Stop executors and release the sync LLM connection pool."""
        self.index_executor.shutdown(wait=True)
        self.organize_executor.shutdown(wait=True)
        close_sync_clients()


def _organize_integer(value: JsonValue) -> int:
    """Apply the original integer conversion to decoded JSON scalar values."""
    if isinstance(value, (str, int, float)):
        return int(value)
    message = "Organize integer field is not a numeric scalar"
    raise TypeError(message)


def parse_organize_message(data: str) -> tuple[int, int, str | None]:
    """Parse current JSON payloads and the legacy workspace-only payload."""
    try:
        payload: JsonValue = json.loads(data)
        if isinstance(payload, dict):
            return (
                _organize_integer(payload["workspace_id"]),
                _organize_integer(payload.get("user_id") or 0),
                str(payload.get("lock_token") or "") or None,
            )
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        pass

    return int(data), 0, None


def dispatch_message(
    runtime: WorkerRuntime,
    message: QueueMessage,
) -> Future[None]:
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

    error_message = f"Unknown task queue: {message.queue_name}"
    raise ValueError(error_message)


def worker_step(
    runtime: WorkerRuntime,
    consumer: RabbitMQTaskConsumer,
    inflight: dict[Future[None], QueueMessage],
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
    polled = consumer.poll(runtime.available_queues())
    if polled is None:
        consumer.idle()
        return
    try:
        if polled.raw_body is not None:
            polled.raw_body.decode("utf-8")
        future = dispatch_message(runtime, polled)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        consumer.park_failed(polled, error)
    else:
        inflight[future] = polled


def run_worker(max_workers: int | None = None) -> None:
    """Consume tasks forever using independent index and organize pools."""
    index_workers = max_workers or max(
        1, int(os.getenv("WORKER_MAX_THREADS", "5")),
    )
    logger.info(
        "Worker started with index_workers=%s, organize_workers=%s",
        index_workers,
        ORGANIZE_WORKERS,
    )

    consumer = RabbitMQTaskConsumer()
    runtime = WorkerRuntime(index_workers=index_workers)
    inflight: dict[Future[None], QueueMessage] = {}
    try:
        while True:
            try:
                worker_step(runtime, consumer, inflight)
            except AMQPError as exc:
                consumer.close()
                logger.error(
                    "RabbitMQ delivery failed; reconnecting (error_type={})",
                    type(exc).__name__,
                )
                time.sleep(RABBITMQ_RECONNECT_DELAY_SECONDS)
    finally:
        consumer.close()
        runtime.shutdown()
