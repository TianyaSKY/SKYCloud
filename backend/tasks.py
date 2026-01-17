import logging
from concurrent.futures import ThreadPoolExecutor

from app import create_app
from app.extensions import redis_client
from app.services.file_service import FILE_PROCESS_QUEUE, ORGANIZE_FILE_QUEUE
from worker.chain_handler import handle_organize_process
from worker.file_handler import handle_file_process

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = create_app()


def process_task(file_id):
    """
    在线程中处理单个文件处理任务
    """
    with app.app_context():
        try:
            handle_file_process(file_id)
        except Exception as e:
            logger.exception(f"Error in thread processing file {file_id}: {e}")


def process_organize_task(user_id):
    """
    在线程中处理文件整理任务
    """
    with app.app_context():
        try:
            logger.info(f"Starting organize_files for user {user_id}")
            result = handle_organize_process(user_id)
            logger.info(f"Finished organize_files for user {user_id}: {result}")
        except Exception as e:
            logger.exception(f"Error in thread organizing files for user {user_id}: {e}")


def run_worker(max_workers):
    logger.info(f"Worker started with {max_workers} threads, waiting for tasks...")

    # 使用线程池处理并发
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            # blpop 返回一个元组 (queue_name, data)
            task = redis_client.blpop([FILE_PROCESS_QUEUE, ORGANIZE_FILE_QUEUE], timeout=0)

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
                    logger.exception(f"Invalid data received from queue {queue_name}: {data}")
                except Exception as e:
                    logger.exception(f"Error submitting task from {queue_name}: {e}")


if __name__ == '__main__':
    run_worker(max_workers=5)
