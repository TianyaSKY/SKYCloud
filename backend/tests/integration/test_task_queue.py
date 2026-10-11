"""Opt-in real broker transport and executor-bound delivery lifecycle checks."""
import base64
import json
import os
import threading
import time
import uuid

import pika
import pytest

from app.infra import task_queue as queue
from app.infra.task_queue import publish_messages as real_publish
from app.infra.worker_runtime import WorkerRuntime, worker_step
from tests.integration.test_runtime_security import docker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TASK_QUEUE_TESTS") != "1", reason="Opt-in isolated RabbitMQ"
)
IMAGE = "rabbitmq:3.13-management-alpine@sha256:606d8c0d6b3c18d1da9afc53bc7cdb2a8d5486df91b5a9830e9e07626c9ae281"


@pytest.fixture
def broker(monkeypatch):
    name = "skycloud-mq-test-" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    for key, value in {"USER": "queue-test", "PASSWORD": password,
                       "HOST": "127.0.0.1", "HEARTBEAT": "2",
                       "CONNECTION_ATTEMPTS": "1"}.items():
        monkeypatch.setenv("RABBITMQ_" + key, value)
    monkeypatch.setattr(queue, "publish_messages", real_publish)
    try:
        docker("run", "-d", "--name", name, "-e", "RABBITMQ_DEFAULT_USER=queue-test",
               "-e", "RABBITMQ_DEFAULT_PASS=" + password,
               "-p", "127.0.0.1::5672", IMAGE)
        port = docker("port", name, "5672/tcp").stdout.strip().rsplit(":", 1)[1]
        monkeypatch.setenv("RABBITMQ_PORT", port)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                connection = queue.open_connection()
                connection.close()
                break
            except pika.exceptions.AMQPError:
                time.sleep(.2)
        else:
            raise AssertionError("Isolated RabbitMQ did not start")
        yield name
    finally:
        docker("rm", "-f", "-v", name, check=False)


def test_confirmed_publish_and_unroutable_rejection(broker):
    real_publish(queue.FILE_PROCESS_QUEUE, [41, 42])
    with pytest.raises(pika.exceptions.UnroutableError):
        real_publish("missing-" + uuid.uuid4().hex, [99])
    consumer = queue.RabbitMQTaskConsumer()
    try:
        messages = [consumer.get_message(queue.FILE_PROCESS_QUEUE) for _ in range(2)]
        assert [m.body for m in messages] == ["41", "42"]
        for message in messages:
            assert consumer.acknowledge(message)
        assert consumer.get_message(queue.FILE_PROCESS_QUEUE) is None
    finally:
        consumer.close()


def test_disconnect_requeues_and_old_tag_cannot_ack_new_delivery(broker):
    real_publish(queue.FILE_PROCESS_QUEUE, [73])
    consumer = queue.RabbitMQTaskConsumer()
    try:
        original = consumer.get_message(queue.FILE_PROCESS_QUEUE)
        consumer.close()
        deadline = time.monotonic() + 5
        replacement = None
        while replacement is None and time.monotonic() < deadline:
            replacement = consumer.get_message(queue.FILE_PROCESS_QUEUE)
        assert replacement.body == "73"
        assert replacement.generation != original.generation
        assert not consumer.acknowledge(original)
        assert consumer.acknowledge(replacement)
        assert consumer.get_message(queue.FILE_PROCESS_QUEUE) is None
    finally:
        consumer.close()


def test_worker_capacity_heartbeat_and_completion_ack(broker, monkeypatch):
    from app.infra import worker_runtime
    started, release = threading.Event(), threading.Event()
    calls = []
    def job(file_id):
        calls.append((file_id, threading.get_ident()))
        started.set()
        assert release.wait(10)
    monkeypatch.setattr(worker_runtime, "process_task", job)
    real_publish(queue.FILE_PROCESS_QUEUE, [1, 2])
    consumer = queue.RabbitMQTaskConsumer(.05)
    runtime = WorkerRuntime(1)
    inflight = {}
    try:
        worker_step(runtime, consumer, inflight)
        assert started.wait(2)
        first = next(iter(inflight.values()))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            worker_step(runtime, consumer, inflight)
        assert len(inflight) == 1
        assert calls[0][1] != threading.get_ident()
        assert consumer._connection.is_open
        assert consumer._channel.queue_declare(queue=queue.FILE_PROCESS_QUEUE, passive=True).method.message_count == 1
        release.set()
        next(iter(inflight)).result(timeout=2)
        worker_step(runtime, consumer, inflight)
        assert all(m.delivery_tag != first.delivery_tag for m in inflight.values())
        deadline = time.monotonic() + 3
        while inflight and time.monotonic() < deadline:
            worker_step(runtime, consumer, inflight)
        assert not inflight
        assert [item[0] for item in calls] == [1, 2]
        assert consumer.get_message(queue.FILE_PROCESS_QUEUE) is None
    finally:
        release.set()
        consumer.close()
        runtime.shutdown()


def test_poison_and_task_error_are_parked_durably(broker, monkeypatch):
    from app.infra import worker_runtime
    def fail(_):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(worker_runtime, "process_task", fail)
    real_publish(queue.FILE_PROCESS_QUEUE, ["invalid-id", 5])
    consumer = queue.RabbitMQTaskConsumer(.01)
    runtime = WorkerRuntime(1)
    inflight = {}
    try:
        worker_step(runtime, consumer, inflight)
        worker_step(runtime, consumer, inflight)
        deadline = time.monotonic() + 3
        while inflight and time.monotonic() < deadline:
            worker_step(runtime, consumer, inflight)
        failed = [consumer.get_message(queue.FAILED_TASK_QUEUE) for _ in range(2)]
        payloads = [json.loads(item.body) for item in failed]
        assert [base64.b64decode(p["body_base64"]) for p in payloads] == [b"invalid-id", b"5"]
        assert [p["error_type"] for p in payloads] == ["ValueError", "RuntimeError"]
        for item in failed:
            consumer.acknowledge(item)
        assert consumer.get_message(queue.FILE_PROCESS_QUEUE) is None
    finally:
        consumer.close()
        runtime.shutdown()


def test_confirmed_messages_survive_broker_restart(broker, monkeypatch):
    real_publish(queue.FILE_PROCESS_QUEUE, [88])
    docker("restart", broker)
    port = docker("port", broker, "5672/tcp").stdout.strip().rsplit(":", 1)[1]
    monkeypatch.setenv("RABBITMQ_PORT", port)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            connection = queue.open_connection()
            connection.close()
            break
        except pika.exceptions.AMQPError:
            time.sleep(.2)
    else:
        raise AssertionError("Isolated RabbitMQ did not restart")
    consumer = queue.RabbitMQTaskConsumer()
    try:
        message = consumer.get_message(queue.FILE_PROCESS_QUEUE)
        assert message.body == "88"
        assert consumer.acknowledge(message)
        assert consumer.get_message(queue.FILE_PROCESS_QUEUE) is None
    finally:
        consumer.close()


def test_actual_auth_rejection_reconnects_without_private_failure_logs(broker, monkeypatch):
    from loguru import logger
    secret = 'private-broker-auth-rejection-sentinel'
    real_publish(queue.FILE_PROCESS_QUEUE, ['retry-after-auth-failure'])
    original = queue.open_connection
    attempts = []
    username, password = os.environ['RABBITMQ_USER'], os.environ['RABBITMQ_PASSWORD']
    def open_after_one_denial():
        attempts.append(1)
        if len(attempts) != 1:
            return original()
        monkeypatch.setenv('RABBITMQ_USER', 'disposable-denied-user')
        monkeypatch.setenv('RABBITMQ_PASSWORD', secret)
        try:
            return original()  # Actual broker rejects SASL credentials.
        finally:
            monkeypatch.setenv('RABBITMQ_USER', username)
            monkeypatch.setenv('RABBITMQ_PASSWORD', password)
    monkeypatch.setattr(queue, 'open_connection', open_after_one_denial)
    monkeypatch.setattr(queue, 'RABBITMQ_RECONNECT_DELAY_SECONDS', 0)
    consumer = queue.RabbitMQTaskConsumer()
    captured = []
    sink = logger.add(lambda message: captured.append((str(message), message.record['exception'])), format='{message}')
    try:
        message = consumer.get_next_message()
        assert message.body == 'retry-after-auth-failure'
        assert len(attempts) == 2 and consumer.acknowledge(message)
        assert any('RabbitMQ polling failed' in message and 'error_type=' in message for message, _ in captured)
        assert all(secret not in message and exception is None for message, exception in captured)
    finally:
        consumer.close()
        logger.remove(sink)
