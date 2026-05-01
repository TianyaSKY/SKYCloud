from __future__ import annotations

import logging
import json
import os
import time
from dataclasses import dataclass
from typing import Iterable

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import AMQPError

logger = logging.getLogger(__name__)

FILE_PROCESS_QUEUE = "file_process_queue"
ORGANIZE_FILE_QUEUE = "organize_file_queue"
TASK_QUEUES = (FILE_PROCESS_QUEUE, ORGANIZE_FILE_QUEUE)

RABBITMQ_RECONNECT_DELAY_SECONDS = float(
    os.getenv("RABBITMQ_RECONNECT_DELAY_SECONDS", "5")
)


@dataclass(frozen=True)
class QueueMessage:
    queue_name: str
    body: str


def _rabbitmq_port() -> int:
    return int(os.getenv("RABBITMQ_PORT", "5672"))


def _connection_parameters() -> pika.connection.Parameters:
    rabbitmq_url = os.getenv("RABBITMQ_URL")
    if rabbitmq_url:
        return pika.URLParameters(rabbitmq_url)

    credentials = pika.PlainCredentials(
        os.getenv("RABBITMQ_USER", "guest"),
        os.getenv("RABBITMQ_PASSWORD", "guest"),
    )
    return pika.ConnectionParameters(
        host=os.getenv("RABBITMQ_HOST", "localhost"),
        port=_rabbitmq_port(),
        virtual_host=os.getenv("RABBITMQ_VHOST", "/"),
        credentials=credentials,
        heartbeat=int(os.getenv("RABBITMQ_HEARTBEAT", "60")),
        blocked_connection_timeout=int(
            os.getenv("RABBITMQ_BLOCKED_CONNECTION_TIMEOUT", "30")
        ),
        connection_attempts=int(os.getenv("RABBITMQ_CONNECTION_ATTEMPTS", "3")),
        retry_delay=RABBITMQ_RECONNECT_DELAY_SECONDS,
    )


def open_connection() -> pika.BlockingConnection:
    return pika.BlockingConnection(_connection_parameters())


def declare_task_queues(channel: BlockingChannel) -> None:
    for queue_name in TASK_QUEUES:
        channel.queue_declare(queue=queue_name, durable=True)


def publish_messages(queue_name: str, messages: Iterable[str | int]) -> None:
    connection = open_connection()
    try:
        channel = connection.channel()
        declare_task_queues(channel)
        properties = pika.BasicProperties(
            content_type="text/plain",
            delivery_mode=2,
        )
        for message in messages:
            channel.basic_publish(
                exchange="",
                routing_key=queue_name,
                body=str(message).encode("utf-8"),
                properties=properties,
                mandatory=True,
            )
    finally:
        if connection.is_open:
            connection.close()


def publish_file_tasks(file_ids: Iterable[int]) -> None:
    publish_messages(FILE_PROCESS_QUEUE, file_ids)


def publish_organize_task(user_id: int, lock_token: str) -> None:
    publish_messages(
        ORGANIZE_FILE_QUEUE,
        [json.dumps({"user_id": user_id, "lock_token": lock_token})],
    )


class RabbitMQTaskConsumer:
    def __init__(self, poll_interval_seconds: float = 0.5):
        self.poll_interval_seconds = poll_interval_seconds
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None
        self._next_queue_index = 0

    def connect(self) -> None:
        self.close()
        self._connection = open_connection()
        self._channel = self._connection.channel()
        declare_task_queues(self._channel)
        logger.info("Connected to RabbitMQ task queues")

    def close(self) -> None:
        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
        finally:
            self._connection = None
            self._channel = None

    def _ensure_channel(self) -> BlockingChannel:
        if (
            self._connection is None
            or self._channel is None
            or self._connection.is_closed
            or self._channel.is_closed
        ):
            self.connect()
        if self._channel is None:
            raise RuntimeError("RabbitMQ channel is not available")
        return self._channel

    def _decode_body(self, body: bytes | str) -> str:
        if isinstance(body, bytes):
            return body.decode("utf-8")
        return body

    def get_message(self, queue_name: str) -> QueueMessage | None:
        channel = self._ensure_channel()
        method_frame, _, body = channel.basic_get(queue=queue_name, auto_ack=True)
        if not method_frame:
            return None
        return QueueMessage(queue_name=queue_name, body=self._decode_body(body))

    def get_next_message(self) -> QueueMessage:
        while True:
            try:
                for offset in range(len(TASK_QUEUES)):
                    queue_index = (self._next_queue_index + offset) % len(TASK_QUEUES)
                    queue_name = TASK_QUEUES[queue_index]
                    message = self.get_message(queue_name)
                    if message:
                        self._next_queue_index = (queue_index + 1) % len(TASK_QUEUES)
                        return message

                if self._connection and self._connection.is_open:
                    self._connection.sleep(self.poll_interval_seconds)
                else:
                    time.sleep(self.poll_interval_seconds)
            except AMQPError:
                self.close()
                logger.exception(
                    "RabbitMQ polling failed; reconnecting in %.1f seconds",
                    RABBITMQ_RECONNECT_DELAY_SECONDS,
                )
                time.sleep(RABBITMQ_RECONNECT_DELAY_SECONDS)

    def drain_messages(self, queue_name: str, max_count: int) -> list[QueueMessage]:
        messages: list[QueueMessage] = []
        for _ in range(max_count):
            message = self.get_message(queue_name)
            if not message:
                break
            messages.append(message)
        return messages
