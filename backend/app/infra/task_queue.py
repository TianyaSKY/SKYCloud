"""RabbitMQ 任务队列：发布文件索引 / 整理任务，以及阻塞式消费轮询。

队列名与 payload 格式需与 backend/tasks.py worker 保持一致；
整理任务使用 JSON ``{user_id, lock_token}``，worker 侧兼容旧版纯 user_id 字符串。
"""

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
    """从队列取出的一条消息。"""

    queue_name: str
    body: str


def _rabbitmq_port() -> int:
    return int(os.getenv("RABBITMQ_PORT", "5672"))


def _connection_parameters() -> pika.connection.Parameters:
    """优先 RABBITMQ_URL；否则拼 host/port/凭证。"""
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
    """打开阻塞连接（发布与消费共用参数）。"""
    return pika.BlockingConnection(_connection_parameters())


def declare_task_queues(channel: BlockingChannel) -> None:
    """声明全部任务队列为 durable，保证 broker 重启后不丢定义。"""
    for queue_name in TASK_QUEUES:
        channel.queue_declare(queue=queue_name, durable=True)


def publish_messages(queue_name: str, messages: Iterable[str | int]) -> None:
    """向指定队列批量发布；delivery_mode=2 持久化消息体。"""
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
    """将文件 ID 发布到索引队列（每 ID 一条消息，便于 worker 批合并）。"""
    publish_messages(FILE_PROCESS_QUEUE, file_ids)


def publish_organize_task(user_id: int, lock_token: str) -> None:
    """发布整理任务；lock_token 用于 worker 侧幂等释放分布式锁。"""
    publish_messages(
        ORGANIZE_FILE_QUEUE,
        [json.dumps({"user_id": user_id, "lock_token": lock_token})],
    )


class RabbitMQTaskConsumer:
    """多队列轮询消费者：空队列 sleep，AMQP 错误后退避重连。"""

    def __init__(self, poll_interval_seconds: float = 0.5):
        self.poll_interval_seconds = poll_interval_seconds
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None
        self._next_queue_index = 0

    def connect(self) -> None:
        """建立连接并声明队列。"""
        self.close()
        self._connection = open_connection()
        self._channel = self._connection.channel()
        declare_task_queues(self._channel)
        logger.info("Connected to RabbitMQ task queues")

    def close(self) -> None:
        """关闭连接；忽略已关闭状态。"""
        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
        finally:
            self._connection = None
            self._channel = None

    def _ensure_channel(self) -> BlockingChannel:
        """断线时自动重连。"""
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
        """非阻塞取一条；auto_ack 简化失败重试策略（任务本身需幂等）。"""
        channel = self._ensure_channel()
        method_frame, _, body = channel.basic_get(queue=queue_name, auto_ack=True)
        if not method_frame:
            return None
        return QueueMessage(queue_name=queue_name, body=self._decode_body(body))

    def get_next_message(self) -> QueueMessage:
        """阻塞轮询全部 TASK_QUEUES，公平轮转避免饿死某一队列。"""
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
        """从单队列尽量再取 max_count 条，供索引批合并。"""
        messages: list[QueueMessage] = []
        for _ in range(max_count):
            message = self.get_message(queue_name)
            if not message:
                break
            messages.append(message)
        return messages
