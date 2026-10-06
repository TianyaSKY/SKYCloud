"""Failure boundaries that must preserve unacknowledged deliveries."""
from concurrent.futures import Future
from unittest.mock import MagicMock

import pika
import pytest

from app.infra import task_queue, worker_runtime
from app.infra.indexing import handler


def test_failed_parking_does_not_ack_until_confirmed(monkeypatch):
    consumer = task_queue.RabbitMQTaskConsumer()
    consumer._channel = MagicMock(is_closed=False)
    message = task_queue.QueueMessage(task_queue.FILE_PROCESS_QUEUE, "12", 1)
    publisher = MagicMock(side_effect=pika.exceptions.AMQPConnectionError)
    monkeypatch.setattr(task_queue, "publish_messages", publisher)
    with pytest.raises(pika.exceptions.AMQPConnectionError):
        consumer.park_failed(message, RuntimeError())
    consumer._channel.basic_ack.assert_not_called()
    consumer._generation += 1
    publisher.reset_mock()
    consumer.park_failed(message, RuntimeError())
    publisher.assert_not_called()


def test_completed_future_is_retained_when_ack_fails():
    consumer = MagicMock()
    consumer.acknowledge.side_effect = pika.exceptions.AMQPConnectionError
    future = Future()
    future.set_result(None)
    message = task_queue.QueueMessage(task_queue.FILE_PROCESS_QUEUE, "12", 1)
    inflight = {future: message}
    with pytest.raises(pika.exceptions.AMQPConnectionError):
        worker_runtime.worker_step(MagicMock(), consumer, inflight)
    assert inflight == {future: message}


@pytest.mark.parametrize("retry_fails", [False, True])
def test_busy_organizer_is_requeued_without_ack_or_quarantine(retry_fails):
    consumer = MagicMock()
    if retry_fails:
        consumer.retry.side_effect = pika.exceptions.AMQPConnectionError
    future = Future()
    future.set_exception(worker_runtime.RetryTaskLater("workspace busy"))
    message = task_queue.QueueMessage(task_queue.ORGANIZE_FILE_QUEUE, "{}", 1)
    inflight = {future: message}
    if retry_fails:
        with pytest.raises(pika.exceptions.AMQPConnectionError):
            worker_runtime.worker_step(MagicMock(), consumer, inflight)
        assert inflight == {future: message}
    else:
        worker_runtime.worker_step(MagicMock(), consumer, inflight)
        assert inflight == {}
        consumer.idle.assert_called_once()
    consumer.retry.assert_called_once_with(message)
    consumer.acknowledge.assert_not_called()
    consumer.park_failed.assert_not_called()


def test_context_load_failure_reaches_queue(monkeypatch):
    monkeypatch.setattr(handler, "load_file_context", MagicMock(side_effect=RuntimeError("database unavailable")))
    with pytest.raises(RuntimeError, match="database unavailable"):
        handler.handle_file_indexing(12)


def test_failure_status_write_failure_reaches_queue(monkeypatch):
    context = MagicMock()
    context.uploader_id = 4
    monkeypatch.setattr(handler, "load_file_context", lambda _: context)
    monkeypatch.setattr(handler, "get_vl_model_config", MagicMock(side_effect=ValueError("provider configuration")))
    monkeypatch.setattr(handler, "mark_file_failed", MagicMock(side_effect=RuntimeError("cannot persist failure")))
    with pytest.raises(RuntimeError, match="cannot persist failure"):
        handler.handle_file_indexing(12)


def test_organize_failure_reaches_queue(monkeypatch):
    lease = MagicMock()
    lease.__enter__.return_value = "claim-token"
    monkeypatch.setattr(worker_runtime, "task_execution", lambda *args, **kwargs: lease)
    monkeypatch.setattr(worker_runtime, "handle_organize_process", MagicMock(side_effect=RuntimeError("organize unavailable")))
    with pytest.raises(RuntimeError, match="organize unavailable"):
        worker_runtime.process_organize_task(3, 4, "owned-token")
    lease.__exit__.assert_called_once()


def test_invalid_utf8_is_parked_before_dispatch(monkeypatch):
    consumer = MagicMock()
    message = task_queue.QueueMessage(task_queue.ORGANIZE_FILE_QUEUE, "{}", 1, raw_body=b'{"lock_token":"\xff"}')
    consumer.poll.return_value = message
    dispatch = MagicMock()
    monkeypatch.setattr(worker_runtime, "dispatch_message", dispatch)
    worker_runtime.worker_step(MagicMock(), consumer, {})
    dispatch.assert_not_called()
    consumer.park_failed.assert_called_once()
    assert isinstance(consumer.park_failed.call_args.args[1], UnicodeDecodeError)
