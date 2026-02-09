import logging
import os
from concurrent.futures import ThreadPoolExecutor

from app import initialize_application
from app.extensions import redis_client, db
from app.services.file_service import FILE_PROCESS_QUEUE, ORGANIZE_FILE_QUEUE
from worker.indexing_handler import handle_file_indexing as handle_file_process
from worker.organize_handler import handle_organize_process

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 初始化应用（数据库等）
initialize_application()


def process_task(file_id):
    """
    在线程中处理单个文件处理任务
    """
    try:
        handle_file_process(file_id)
    except Exception as e:
        logger.exception(f"Error in thread processing file {file_id}: {e}")
    finally:
        db.session.remove()


def process_organize_task(user_id):
    """
    在线程中处理文件整理任务
    """
    try:
        logger.info(f"Starting organize_files for user {user_id}")
        result = handle_organize_process(user_id)
        logger.info(f"Finished organize_files for user {user_id}: {result}")
    except Exception as e:
        logger.exception(f"Error in thread organizing files for user {user_id}: {e}")
    finally:
        db.session.remove()


def run_worker(max_workers):
    logger.info(f"Worker started with {max_workers} threads, waiting for tasks...")

    # 使用线程池处理并发
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            # blpop 返回一个元组 (queue_name, data)
            task = redis_client.blpop(
                [FILE_PROCESS_QUEUE, ORGANIZE_FILE_QUEUE], timeout=0
            )

            if task:
                queue_name, data = task
                try:
                    if queue_name == FILE_PROCESS_QUEUE:
                        file_id = int(data)
                        executor.submit(process_task, file_id)
                    elif queue_name == ORGANIZE_FILE_QUEUE:
                        user_id = int(data)
                        executor.submit(process_organize_task, user_id)
                except ValueError:
                    logger.exception(
                        f"Invalid data received from queue {queue_name}: {data}"
                    )
                except Exception as e:
                    logger.exception(f"Error submitting task from {queue_name}: {e}")


if __name__ == "__main__":
    # 从环境变量读取线程数，默认为 5
    max_workers_env = int(os.getenv("WORKER_MAX_THREADS", 5))
    run_worker(max_workers=max_workers_env)
