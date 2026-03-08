import logging
import os
import time
import threading
import json
from concurrent.futures import ThreadPoolExecutor

from app import initialize_application
from app.extensions import redis_client, db
from app.services import folder_service
from app.services.file_service import (
    FILE_PROCESS_QUEUE,
    ORGANIZE_FILE_QUEUE,
    cleanup_expired_uploads,
)
from worker.indexing_handler import handle_file_indexing as handle_file_process
from worker.indexing_handler import handle_batch_indexing
from worker.organize_handler import handle_organize_process

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 初始化应用（数据库等）
initialize_application()

# 批量处理配置
BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", 10))


def run_scheduler():
    """定时任务调度器"""
    logger.info("Scheduler thread started")
    while True:
        try:
            # 每小时清理一次过期分片
            cleanup_expired_uploads(max_age_hours=24)
        except Exception as e:
            logger.exception(f"Error in scheduler: {e}")

        # 休眠 1 小时 (3600 秒)
        time.sleep(3600)


def process_task(file_id, semaphore: threading.Semaphore):
    """
    在线程中处理单个文件处理任务
    """
    try:
        handle_file_process(file_id)
    except Exception as e:
        logger.exception(f"Error in thread processing file {file_id}: {e}")
    finally:
        db.session.remove()
        semaphore.release()


def drain_file_queue(max_batch: int) -> list[int]:
    """从 Redis 一次性取最多 max_batch 个 file_id（不阻塞）"""
    ids = []
    for _ in range(max_batch):
        data = redis_client.lpop(FILE_PROCESS_QUEUE)
        if data is None:
            break
        ids.append(int(data))
    return ids


def process_batch_task(file_ids: list[int], semaphore: threading.Semaphore):
    """
    在线程中批量处理文件：各自生成描述后统一 batch embedding
    """
    try:
        logger.info(
            f"[Batch] Processing batch of {len(file_ids)} files: {file_ids}")
        handle_batch_indexing(file_ids)
        logger.info(
            f"[Batch] Batch processing completed for {len(file_ids)} files.")
    except Exception as e:
        logger.exception(f"Error in batch processing files {file_ids}: {e}")
    finally:
        db.session.remove()
        semaphore.release()


def process_organize_task(user_id: int, lock_token: str | None = None,
                          semaphore: threading.Semaphore | None = None):
    """
    在线程中处理文件整理任务
    """
    token = lock_token or f"legacy-{user_id}"
    try:
        folder_service.mark_organize_task_running(user_id, token)
        logger.info(f"Starting organize_files for user {user_id}")
        result = handle_organize_process(user_id)
        logger.info(f"Finished organize_files for user {user_id}: {result}")
    except Exception as e:
        logger.exception(
            f"Error in thread organizing files for user {user_id}: {e}")
    finally:
        folder_service.release_organize_task_lock(user_id, token)
        db.session.remove()
        if semaphore:
            semaphore.release()


def run_worker(max_workers):
    logger.info(
        f"Worker started with {max_workers} threads, batch_size={BATCH_SIZE}, waiting for tasks...")

    # 信号量：限制同时运行的任务数，防止主循环提交速度超过处理速度
    semaphore = threading.Semaphore(max_workers)

    # 使用线程池处理并发
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            # 先获取信号量（如果所有线程都忙，这里会阻塞等待）
            semaphore.acquire()

            try:
                # blpop 返回一个元组 (queue_name, data)
                task = redis_client.blpop(
                    [FILE_PROCESS_QUEUE, ORGANIZE_FILE_QUEUE], timeout=0
                )

                if task:
                    queue_name, data = task
                    try:
                        if queue_name == FILE_PROCESS_QUEUE:
                            # 取到第一个 file_id 后，尝试 drain 更多
                            first_id = int(data)
                            extra_ids = drain_file_queue(BATCH_SIZE - 1)
                            all_ids = [first_id] + extra_ids

                            if len(all_ids) > 1:
                                # 多个文件：用批量处理
                                logger.info(
                                    f"[Worker] Submitting batch of {len(all_ids)} files")
                                executor.submit(
                                    process_batch_task, all_ids, semaphore)
                            else:
                                # 单个文件：保持原有逻辑
                                executor.submit(
                                    process_task, first_id, semaphore)
                        elif queue_name == ORGANIZE_FILE_QUEUE:
                            lock_token = None
                            user_id = None
                            try:
                                payload = json.loads(data)
                                if isinstance(payload, dict):
                                    user_id = int(payload["user_id"])
                                    lock_token = str(
                                        payload.get("lock_token") or "")
                            except (json.JSONDecodeError, TypeError, KeyError, ValueError):
                                user_id = int(data)

                            if user_id is None:
                                raise ValueError(
                                    f"Invalid organize queue payload: {data}")

                            executor.submit(process_organize_task,
                                            user_id, lock_token, semaphore)
                    except ValueError:
                        logger.exception(
                            f"Invalid data received from queue {queue_name}: {data}"
                        )
                        semaphore.release()  # 提交失败时释放信号量
                    except Exception as e:
                        logger.exception(
                            f"Error submitting task from {queue_name}: {e}")
                        semaphore.release()  # 提交失败时释放信号量
                else:
                    semaphore.release()  # blpop 超时无数据时释放信号量
            except Exception:
                semaphore.release()  # 任何异常都释放信号量
                raise


if __name__ == "__main__":
    # 启动定时清理线程（作为守护线程）
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # 从环境变量读取线程数，默认为 5
    max_workers_env = int(os.getenv("WORKER_MAX_THREADS", 5))
    run_worker(max_workers=max_workers_env)
