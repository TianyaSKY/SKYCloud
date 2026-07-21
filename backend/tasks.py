"""Worker 进程入口：消费 RabbitMQ 任务（文件索引 / 目录整理）并定时清理。

职责边界：
- 本文件只做调度：取消息、批量合并、线程池提交、session/信号量收尾
- 业务逻辑在 app.workers.* 与 app.services.*
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app import initialize_application
from app.infra.task_queue import (
    FILE_PROCESS_QUEUE,
    ORGANIZE_FILE_QUEUE,
    QueueMessage,
    RabbitMQTaskConsumer,
)
from app.services import folder_service
from app.services.file_service import cleanup_expired_uploads
from app.workers.indexing_handler import handle_batch_indexing
from app.workers.organize_handler import handle_organize_process

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

initialize_application()

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "10"))
MAX_WORKERS = int(os.getenv("WORKER_MAX_THREADS", "5"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("WORKER_CLEANUP_INTERVAL_SECONDS", "3600"))
SUBMIT_ERROR_BACKOFF_SECONDS = float(os.getenv("WORKER_SUBMIT_ERROR_BACKOFF", "1"))


# ---------------------------------------------------------------------------
# 定时任务
# ---------------------------------------------------------------------------


def run_scheduler() -> None:
    """定时清理过期分片上传，失败不退出进程。"""
    logger.info("Scheduler thread started (interval=%ss)", CLEANUP_INTERVAL_SECONDS)
    while True:
        try:
            cleanup_expired_uploads(max_age_hours=24)
        except Exception:
            logger.exception("Scheduler cleanup failed")
        time.sleep(CLEANUP_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# 任务执行（线程池内）
# ---------------------------------------------------------------------------


def _finish_slot(semaphore: threading.Semaphore) -> None:
    """线程任务收尾：归还并发槽位。"""
    semaphore.release()


def process_indexing_task(file_ids: list[int], semaphore: threading.Semaphore) -> None:
    """索引一批文件（含单文件）；描述逐个生成，embedding 批量调用。"""
    try:
        logger.info("Indexing %d file(s): %s", len(file_ids), file_ids)
        handle_batch_indexing(file_ids)
        logger.info("Indexing finished for %d file(s)", len(file_ids))
    except Exception:
        logger.exception("Indexing failed for files %s", file_ids)
    finally:
        _finish_slot(semaphore)


def process_organize_task(
        user_id: int,
        lock_token: str,
        semaphore: threading.Semaphore,
) -> None:
    """执行用户目录整理，并始终释放分布式锁。"""
    token = lock_token or f"legacy-{user_id}"
    try:
        folder_service.mark_organize_task_running(user_id, token)
        logger.info("Organize started for user_id=%s", user_id)
        result = handle_organize_process(user_id)
        logger.info("Organize finished for user_id=%s: %s", user_id, result)
    except Exception:
        logger.exception("Organize failed for user_id=%s", user_id)
    finally:
        try:
            folder_service.release_organize_task_lock(user_id, token)
        except Exception:
            logger.exception("Failed to release organize lock user_id=%s", user_id)
        _finish_slot(semaphore)


# ---------------------------------------------------------------------------
# 消息解析与分发
# ---------------------------------------------------------------------------


def _parse_organize_payload(body: str) -> tuple[int, str]:
    """解析整理队列消息，兼容 JSON 与旧版纯 user_id 字符串。"""
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            user_id = int(payload["user_id"])
            lock_token = str(payload.get("lock_token") or "") or f"legacy-{user_id}"
            return user_id, lock_token
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        pass
    user_id = int(body)
    return user_id, f"legacy-{user_id}"


def _collect_file_ids(consumer: RabbitMQTaskConsumer, first_body: str) -> list[int]:
    """将当前消息与同队列后续消息合并为一批（最多 BATCH_SIZE）。"""
    file_ids = [int(first_body)]
    for message in consumer.drain_messages(FILE_PROCESS_QUEUE, max(0, BATCH_SIZE - 1)):
        file_ids.append(int(message.body))
    return file_ids


def _submit_message(
        message: QueueMessage,
        consumer: RabbitMQTaskConsumer,
        executor: ThreadPoolExecutor,
        semaphore: threading.Semaphore,
) -> None:
    """按队列类型提交任务；成功后由任务线程释放 semaphore。"""
    if message.queue_name == FILE_PROCESS_QUEUE:
        file_ids = _collect_file_ids(consumer, message.body)
        executor.submit(process_indexing_task, file_ids, semaphore)
        return

    if message.queue_name == ORGANIZE_FILE_QUEUE:
        user_id, lock_token = _parse_organize_payload(message.body)
        executor.submit(process_organize_task, user_id, lock_token, semaphore)
        return

    raise ValueError(f"Unknown queue: {message.queue_name}")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def run_worker(max_workers: int = MAX_WORKERS) -> None:
    """阻塞运行：信号量限流 + 线程池执行，单条失败不退出进程。"""
    logger.info(
        "Worker started: threads=%s batch_size=%s",
        max_workers,
        BATCH_SIZE,
    )
    semaphore = threading.Semaphore(max_workers)
    consumer = RabbitMQTaskConsumer()

    with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="skycloud-worker",
    ) as executor:
        while True:
            # 槽位占满时阻塞，避免无限堆积 future
            semaphore.acquire()
            try:
                message = consumer.get_next_message()
                _submit_message(message, consumer, executor, semaphore)
            except Exception:
                # 取消息/提交失败：归还槽位并继续，避免整个 worker 挂掉
                logger.exception(
                    "Failed to fetch or submit task; retry in %.1fs",
                    SUBMIT_ERROR_BACKOFF_SECONDS,
                )
                semaphore.release()
                time.sleep(SUBMIT_ERROR_BACKOFF_SECONDS)


if __name__ == "__main__":
    threading.Thread(
        target=run_scheduler,
        name="upload-cleanup-scheduler",
        daemon=True,
    ).start()
    run_worker()
