"""必须保留未确认投递的故障边界测试."""
from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pika.exceptions import AMQPConnectionError

from app.infra import task_queue, worker_runtime
from app.infra.indexing import handler
from tests.test_llm_safe_diagnostics import PRIVATE, assert_safe, logs

_ = logs


def test_failed_parking_does_not_ack_until_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    consumer = task_queue.RabbitMQTaskConsumer()
    consumer._channel = MagicMock(is_closed=False)  # pyright: ignore[reportPrivateUsage]
    message = task_queue.QueueMessage(task_queue.FILE_PROCESS_QUEUE, "12", 1)
    publisher = MagicMock(side_effect=AMQPConnectionError)
    monkeypatch.setattr(task_queue, "publish_messages", publisher)
    with pytest.raises(AMQPConnectionError):
        consumer.park_failed(message, RuntimeError())
    consumer._channel.basic_ack.assert_not_called()  # pyright: ignore[reportPrivateUsage]
    consumer._generation += 1  # pyright: ignore[reportPrivateUsage]
    publisher.reset_mock()
    consumer.park_failed(message, RuntimeError())
    publisher.assert_not_called()


def test_completed_future_is_retained_when_ack_fails() -> None:
    consumer = MagicMock()
    consumer.acknowledge.side_effect = AMQPConnectionError
    future: Future[None] = Future()
    future.set_result(None)
    message = task_queue.QueueMessage(task_queue.FILE_PROCESS_QUEUE, "12", 1)
    inflight: dict[Future[None], task_queue.QueueMessage] = {future: message}
    with pytest.raises(AMQPConnectionError):
        worker_runtime.worker_step(MagicMock(), consumer, inflight)
    assert inflight == {future: message}


@pytest.mark.parametrize("retry_fails", [False, True])
def test_busy_organizer_is_requeued_without_ack_or_quarantine(retry_fails: bool) -> None:
    consumer = MagicMock()
    if retry_fails:
        consumer.retry.side_effect = AMQPConnectionError
    future: Future[None] = Future()
    future.set_exception(worker_runtime.RetryTaskLater("workspace busy"))
    message = task_queue.QueueMessage(task_queue.ORGANIZE_FILE_QUEUE, "{}", 1)
    inflight: dict[Future[None], task_queue.QueueMessage] = {future: message}
    if retry_fails:
        with pytest.raises(AMQPConnectionError):
            worker_runtime.worker_step(MagicMock(), consumer, inflight)
        assert inflight == {future: message}
    else:
        worker_runtime.worker_step(MagicMock(), consumer, inflight)
        assert inflight == {}
        consumer.idle.assert_called_once()
    consumer.retry.assert_called_once_with(message)
    consumer.acknowledge.assert_not_called()
    consumer.park_failed.assert_not_called()


def test_context_load_failure_reaches_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "load_file_context", MagicMock(side_effect=RuntimeError("database unavailable")))
    with pytest.raises(RuntimeError, match="database unavailable"):
        handler.handle_file_indexing(12)


def test_failure_status_write_failure_reaches_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    context = MagicMock()
    context.uploader_id = 4

    def mock_load_context(_: object) -> MagicMock:
        return context

    monkeypatch.setattr(handler, "load_file_context", mock_load_context)
    monkeypatch.setattr(handler, "get_vl_model_config", MagicMock(side_effect=ValueError("provider configuration")))
    monkeypatch.setattr(handler, "mark_file_failed", MagicMock(side_effect=RuntimeError("cannot persist failure")))
    with pytest.raises(RuntimeError, match="cannot persist failure"):
        handler.handle_file_indexing(12)


def test_organize_failure_reaches_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    lease = MagicMock()
    lease.__enter__.return_value = "claim-token"

    def mock_task_execution(*args: object, **kwargs: object) -> MagicMock:
        return lease

    monkeypatch.setattr(worker_runtime, "task_execution", mock_task_execution)
    monkeypatch.setattr(worker_runtime, "handle_organize_process", MagicMock(side_effect=RuntimeError("organize unavailable")))
    with pytest.raises(RuntimeError, match="organize unavailable"):
        worker_runtime.process_organize_task(3, 4, "owned-token")
    lease.__exit__.assert_called_once()


def test_invalid_utf8_is_parked_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    consumer = MagicMock()
    message = task_queue.QueueMessage(task_queue.ORGANIZE_FILE_QUEUE, "{}", 1, raw_body=b'{"lock_token":"\xff"}')
    consumer.poll.return_value = message
    dispatch = MagicMock()
    monkeypatch.setattr(worker_runtime, "dispatch_message", dispatch)
    worker_runtime.worker_step(MagicMock(), consumer, {})
    dispatch.assert_not_called()
    consumer.park_failed.assert_called_once()
    assert isinstance(consumer.park_failed.call_args.args[1], UnicodeDecodeError)


def test_scheduler_failure_preserves_later_actions_and_next_tick(
    monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    events: list[str] = []
    file_tasks = [11]
    organize_tasks = [21, 22]
    delivered: list[tuple[str, int]] = []
    attempts = 0
    ticks = 0

    class StopScheduler(BaseException):
        pass

    def deliver_files() -> None:
        nonlocal attempts
        attempts += 1
        events.append("file_attempt")
        if attempts == 1:
            raise RuntimeError(PRIVATE)
        delivered.append(("file", file_tasks.pop(0)))

    def deliver_organize() -> None:
        events.append("organize_delivery")
        delivered.append(("organize", organize_tasks.pop(0)))

    def maintenance(name: str) -> Callable[[], None]:
        def action() -> None:
            events.append(name)
        return action

    @contextmanager
    def session_factory() -> Generator[object, None, None]:
        session = object()
        events.append("session_open")
        try:
            yield session
        finally:
            events.append("session_closed")

    def cleanup_uploads(*, max_age_hours: int, session: object) -> int:
        assert max_age_hours == 24
        assert session is not None
        events.append("uploads_cleaned")
        return 1

    def advance_tick(seconds: float) -> None:
        nonlocal ticks
        assert seconds == 1
        ticks += 1
        events.append("tick")
        if ticks == 2:
            raise StopScheduler

    monkeypatch.setattr(worker_runtime, "deliver_pending_file_tasks", deliver_files)
    monkeypatch.setattr(worker_runtime, "deliver_pending_organize_tasks", deliver_organize)
    for name in (
        "delete_pending_objects",
        "delete_pending_containers",
        "retire_cancelled_operations",
        "recover_runtime_operations",
        "recover_abandoned_runs",
        "abort_pending_sessions",
    ):
        monkeypatch.setattr(worker_runtime, name, maintenance(name))
    monkeypatch.setattr(worker_runtime, "SessionLocal", session_factory)
    monkeypatch.setattr(worker_runtime, "cleanup_expired_uploads", cleanup_uploads)
    monkeypatch.setattr(
        worker_runtime,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=advance_tick),
    )

    with pytest.raises(StopScheduler):
        worker_runtime.run_scheduler()

    maintenance_events = [
        "file_attempt",
        "organize_delivery",
        "delete_pending_objects",
        "delete_pending_containers",
        "retire_cancelled_operations",
        "recover_runtime_operations",
        "recover_abandoned_runs",
        "abort_pending_sessions",
    ]
    assert events == (
        maintenance_events
        + ["session_open", "uploads_cleaned", "session_closed", "tick"]
        + maintenance_events
        + ["tick"]
    )
    assert delivered == [("organize", 21), ("file", 11), ("organize", 22)]
    assert file_tasks == organize_tasks == []
    assert_safe(
        logs,
        "Error delivering pending file tasks (error_type=RuntimeError)",
    )
