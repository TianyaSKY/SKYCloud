"""
文件索引处理器

处理文件的 AI 索引任务：
1. 使用 VL 模型生成文件描述
2. 将描述转换为向量嵌入
3. 存储到数据库供语义搜索使用
"""

import datetime
import logging

from app.extensions import db
from app.models.file import File
from app.services import file_service, inbox_service
from app.services.model_config import get_embedding_model_config, get_vl_model_config
from worker.description_generator import generate_file_description

logger = logging.getLogger(__name__)


def handle_file_indexing(file_id: int) -> None:
    """
    处理单个文件的索引任务。

    流程：
    1. 获取文件记录，更新状态为 processing
    2. 调用 VL 模型生成文件描述
    3. 调用 Embedding 模型生成向量
    4. 更新数据库，标记为 success
    5. 失败时发送通知给用户

    Args:
        file_id: 待处理的文件 ID
    """
    try:
        file: File = file_service.get_file(file_id)
        if not file:
            logger.error(f"File ID {file_id} not found.")
            return

        logger.info(f"Starting to index file: {file.name} (ID: {file_id})")

        # 更新状态为处理中
        file.status = "processing"
        db.session.commit()

        # 获取模型配置
        vl_config = get_vl_model_config()
        emb_config = get_embedding_model_config()

        # 使用 get_abs_path 获取完整路径
        abs_path = file.get_abs_path()

        # 生成描述
        description = generate_file_description(abs_path, vl_config)
        file.description = description
        db.session.commit()

        # 生成向量嵌入
        file.vector_info = file_service.embedding_desc(description, emb_config)

        # 更新状态为成功
        file.status = "success"
        db.session.commit()
        logger.info(f"Finished indexing file ID: {file_id} successfully.")

    except Exception as e:
        logger.error(f"Error indexing file {file_id}: {e}")
        db.session.rollback()

        file = File.query.get(file_id)
        if file:
            file.status = "fail"
            db.session.commit()

            # 发送信息给用户
            inbox_service.create_inbox_message(
                {
                    "type": "system",
                    "user_id": file.uploader_id,
                    "title": "文件处理失败",
                    "content": (
                        "处理文件时出现了错误\n"
                        f"时间:{datetime.datetime.now()}\n"
                        f"文件id:{file_id}\n"
                        f"{e}\n"
                    ),
                }
            )


# 为了向后兼容，保留旧函数名的别名
handle_file_process = handle_file_indexing
