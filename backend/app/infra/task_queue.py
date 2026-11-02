"""RabbitMQ 任务队列: 发布文件索引 / 整理任务, 以及阻塞式消费轮询.

整理任务使用 workspace_id/user_id/lock_token JSON, worker 兼容旧版 workspace_id 字符串.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import unquote, urlsplit

import pika
from loguru import logger
from pika.exceptions import AMQPError

from app.infra.secrets import configured_password

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection
    from pika.connection import Parameters

FILE_PROCESS_QUEUE = "file_process_queue"
ORGANIZE_FILE_QUEUE = "organize_file_queue"
FAILED_TASK_QUEUE = "failed_task_queue"
TASK_QUEUES = (FILE_PROCESS_QUEUE, ORGANIZE_FILE_QUEUE)

RABBITMQ_RECONNECT_DELAY_SECONDS = float(
    os.getenv("RABBITMQ_RECONNECT_DELAY_SECONDS", "5"),
)


class OrganizeTaskMessage(TypedDict):
    """描述现有的整理任务 JSON 消息体结构."""

    workspace_id: int
    user_id: int
    lock_token: str


class FailedTaskMessage(TypedDict):
    """描述失败队列投递的持久化副本数据结构."""

    queue: str
    body_base64: str
    error_type: str


@dataclass(frozen=True)
class QueueMessage:
    """从队列取出的一条消息."""

    queue_name: str
    body: str
    delivery_tag: int | None = None
    generation: int = 0
    raw_body: bytes | None = None


def _rabbitmq_port() -> int:
    return int(os.getenv("RABBITMQ_PORT", "5672"))


def _connection_parameters() -> Parameters:
    """优先 RABBITMQ_URL; 否则拼 host/port/凭证."""
    rabbitmq_url = os.getenv("RABBITMQ_URL")
    if rabbitmq_url:
        configured_password(
            "RabbitMQ URL password",
            unquote(urlsplit(rabbitmq_url).password or ""),
        )
        return pika.URLParameters(rabbitmq_url)

    credentials = pika.PlainCredentials(
        os.getenv("RABBITMQ_USER", "skycloud"),
        configured_password("RABBITMQ_PASSWORD", os.getenv("RABBITMQ_PASSWORD")),
    )
    return pika.ConnectionParameters(
        host=os.getenv("RABBITMQ_HOST", "localhost"),
        port=_rabbitmq_port(),
        virtual_host=os.getenv("RABBITMQ_VHOST", "/"),
        credentials=credentials,
        heartbeat=int(os.getenv("RABBITMQ_HEARTBEAT", "60")),
        blocked_connection_timeout=int(
            os.getenv("RABBITMQ_BLOCKED_CONNECTION_TIMEOUT", "30"),
        ),
        connection_attempts=int(os.getenv("RABBITMQ_CONNECTION_ATTEMPTS", "3")),
        retry_delay=RABBITMQ_RECONNECT_DELAY_SECONDS,
    )


def open_connection() -> BlockingConnection:
    """打开阻塞连接(发布与消费共用参数)."""
    return pika.BlockingConnection(_connection_parameters())


def declare_task_queues(channel: BlockingChannel) -> None:
    """声明全部任务队列为 durable, 保证 broker 重启后不丢定义."""
    for queue_name in (*TASK_QUEUES, FAILED_TASK_QUEUE):
        channel.queue_declare(queue=queue_name, durable=True)


def publish_messages(queue_name: str, messages: Iterable[str | int]) -> None:
    """向指定队列批量发布; delivery_mode=2 持久化消息体."""
    connection = open_connection()
    try:
        channel = connection.channel()
        declare_task_queues(channel)
        channel.confirm_delivery()
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
    """将文件 ID 发布到索引队列(每 ID 一条消息, worker 逐条独立处理)."""
    publish_messages(FILE_PROCESS_QUEUE, file_ids)


def publish_organize_task(workspace_id: int, user_id: int, lock_token: str) -> None:
    """发布整理任务; lock_token 用于 worker 侧幂等释放分布式锁."""
    payload: OrganizeTaskMessage = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "lock_token": lock_token,
    }
    publish_messages(ORGANIZE_FILE_QUEUE, [json.dumps(payload)])


class RabbitMQTaskConsumer:
    """多队列轮询消费者: 空队列 sleep, AMQP 错误后退避重连."""

    def __init__(self, poll_interval_seconds: float = 0.5) -> None:
        """创建消费者实例而不立即建立延迟代理连接."""
        self.poll_interval_seconds = poll_interval_seconds
        self._connection: BlockingConnection | None = None
        self._channel: BlockingChannel | None = None
        self._next_queue_index = 0
        self._generation = 0

    def connect(self) -> None:
        """建立连接并声明队列."""
        self.close()
        self._connection = open_connection()
        self._channel = self._connection.channel()
        declare_task_queues(self._channel)
        self._generation += 1
        logger.info("Connected to RabbitMQ task queues")

    def close(self) -> None:
        """关闭连接; 忽略已关闭状态."""
        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
        except Exception as exc:
            # Pika 清理跨越传输层回调; 即使不可穷举的传输/协议
            # 失败中断了关闭过程, 清理逻辑也必须丢弃连接句柄。
            logger.warning(
                "RabbitMQ connection close failed: error_type={}",
                type(exc).__name__,
            )
        finally:
            self._connection = None
            self._channel = None

    def _ensure_channel(self) -> BlockingChannel:
        """断线时自动重连."""
        if (
            self._connection is None
            or self._channel is None
            or self._connection.is_closed
            or self._channel.is_closed
        ):
            self.connect()
        if self._channel is None:
            message = "RabbitMQ channel is not available"
            raise RuntimeError(message)
        return self._channel

    def _require_connection(self) -> BlockingConnection:
        """在需要时重连并返回活跃连接."""
        self._ensure_channel()
        if self._connection is None:
            message = "RabbitMQ connection is not available"
            raise RuntimeError(message)
        return self._connection

    def _decode_body(self, body: bytes | str) -> str:
        if isinstance(body, bytes):
            return body.decode("utf-8")
        return body

    def get_message(self, queue_name: str) -> QueueMessage | None:
        """取出但不确认; 仅在任务持久化完成后确认."""
        channel = self._ensure_channel()
        method_frame, _, body = channel.basic_get(queue=queue_name, auto_ack=False)
        if not method_frame or body is None:
            return None
        # types-pika 将 body 标注为 str, 但阻塞 channel 在
        # auto_ack=False 时返回 bytes; 规范化两者。
        raw_body = body if isinstance(body, bytes) else body.encode("utf-8")
        return QueueMessage(
            queue_name=queue_name,
            body=raw_body.decode("utf-8", errors="replace"),
            delivery_tag=method_frame.delivery_tag,
            generation=self._generation,
            raw_body=raw_body,
        )

    def _owned_delivery(
        self,
        message: QueueMessage,
    ) -> tuple[BlockingChannel, int] | None:
        """返回活跃的投递元组 (channel, tag), 若所有权丢失则返回 None."""
        channel = self._channel
        if (
            message.generation != self._generation
            or channel is None
            or channel.is_closed
            or message.delivery_tag is None
        ):
            return None
        return channel, message.delivery_tag

    def owns_delivery(self, message: QueueMessage) -> bool:
        """返回当前通道是否仍然拥有该投递."""
        return self._owned_delivery(message) is not None

    def acknowledge(self, message: QueueMessage) -> bool:
        """仅在接收通道上确认; 旧的 tag 无法在重连后继续存活."""
        owned = self._owned_delivery(message)
        if owned is None:
            return False
        channel, delivery_tag = owned
        channel.basic_ack(delivery_tag=delivery_tag)
        return True

    def retry(self, message: QueueMessage) -> bool:
        """将繁忙或瞬态失败的任务重新放回队列以供再次尝试."""
        owned = self._owned_delivery(message)
        if owned is None:
            return False
        channel, delivery_tag = owned
        channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
        return True

    def park_failed(self, message: QueueMessage, error: BaseException) -> None:
        """持久化保存失败的载荷; 仅在 broker 确认副本后才执行 ack."""
        if not self.owns_delivery(message):
            return
        payload: FailedTaskMessage = {
            "queue": message.queue_name,
            "body_base64": base64.b64encode(
                message.raw_body or message.body.encode("utf-8"),
            ).decode("ascii"),
            "error_type": type(error).__name__,
        }
        publish_messages(FAILED_TASK_QUEUE, [json.dumps(payload)])
        self.acknowledge(message)

    def poll(self, queue_names: Iterable[str]) -> QueueMessage | None:
        """单轮公平轮询; 调用方可在各轮之间处理 futures."""
        enabled = set(queue_names)
        for offset in range(len(TASK_QUEUES)):
            index = (self._next_queue_index + offset) % len(TASK_QUEUES)
            if TASK_QUEUES[index] not in enabled:
                continue
            message = self.get_message(TASK_QUEUES[index])
            if message is not None:
                self._next_queue_index = (index + 1) % len(TASK_QUEUES)
                return message
        return None

    def idle(self) -> None:
        """即使所有执行器槽位均被占用, 也保持心跳维护."""
        self._require_connection().sleep(self.poll_interval_seconds)

    def get_next_message(self) -> QueueMessage:
        """阻塞轮询全部 TASK_QUEUES, 公平轮转避免饿死某一队列."""
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
            except AMQPError as exc:
                self.close()
                logger.error(
                    "RabbitMQ polling failed; reconnecting in {} seconds: "
                    "error_type={}",
                    RABBITMQ_RECONNECT_DELAY_SECONDS,
                    type(exc).__name__,
                )
                time.sleep(RABBITMQ_RECONNECT_DELAY_SECONDS)

    def drain_messages(self, queue_name: str, max_count: int) -> list[QueueMessage]:
        """从单队列尽量再取 max_count 条, 供索引批合并."""
        messages: list[QueueMessage] = []
        for _ in range(max_count):
            message = self.get_message(queue_name)
            if not message:
                break
            messages.append(message)
        return messages
