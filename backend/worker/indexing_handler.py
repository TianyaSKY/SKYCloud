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
from app.services.model_config import (
    get_chat_model_config,
    get_embedding_model_config,
    get_vl_model_config,
)
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
        chat_config = get_chat_model_config()
        emb_config = get_embedding_model_config()

        # 使用 get_abs_path 获取完整路径
        abs_path = file.get_abs_path()

        # 生成描述（纯文本文件使用 chat 模型，其他使用 VL 模型）
        description = generate_file_description(
            abs_path, vl_config, chat_config)
        file.description = description
        db.session.commit()

        # 生成向量嵌入（文件名 + 描述 拼接，使文件名也参与语义检索）
        embedding_text = f"文件名: {file.name}\n{description}"
        file.vector_info = file_service.embedding_desc(
            embedding_text, emb_config)

        # 更新状态为成功
        file.status = "success"
        db.session.commit()
        logger.info(f"Finished indexing file ID: {file_id} successfully.")

    except Exception as e:
        logger.error(f"Error indexing file {file_id}: {e}")
        db.session.rollback()

        file = db.session.get(File, file_id)
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


def _mark_file_failed(file_id: int, error: Exception) -> None:
    """将文件标记为处理失败并发送通知"""
    try:
        db.session.rollback()
        file = db.session.get(File, file_id)
        if file:
            file.status = "fail"
            db.session.commit()

            inbox_service.create_inbox_message(
                {
                    "type": "system",
                    "user_id": file.uploader_id,
                    "title": "文件处理失败",
                    "content": (
                        "处理文件时出现了错误\n"
                        f"时间:{datetime.datetime.now()}\n"
                        f"文件id:{file_id}\n"
                        f"{error}\n"
                    ),
                }
            )
    except Exception as inner_e:
        logger.error(f"Failed to mark file {file_id} as failed: {inner_e}")
        db.session.rollback()


def handle_batch_indexing(file_ids: list[int]) -> None:
    """
    批量处理文件索引：VL 描述各自生成，Embedding 统一批量调用。

    流程：
    1. 逐个文件调用 VL 模型生成描述
    2. 收集所有成功生成描述的文件
    3. 一次性调用 batch_embedding_desc 批量生成向量
    4. 逐个写回数据库，更新状态

    Args:
        file_ids: 待处理的文件 ID 列表
    """
    if not file_ids:
        return

    vl_config = get_vl_model_config()
    chat_config = get_chat_model_config()
    emb_config = get_embedding_model_config()

    # Phase 1: 逐个生成描述
    described_files: list[tuple[File, str]] = []
    for file_id in file_ids:
        try:
            file: File = file_service.get_file(file_id)
            if not file:
                logger.error(f"File ID {file_id} not found.")
                continue

            logger.info(
                f"[Batch] Starting description for: {file.name} (ID: {file_id})")
            file.status = "processing"
            db.session.commit()

            abs_path = file.get_abs_path()
            description = generate_file_description(
                abs_path, vl_config, chat_config)
            file.description = description
            db.session.commit()

            described_files.append((file, description))
            logger.info(
                f"[Batch] Description generated for file ID: {file_id}")

        except Exception as e:
            logger.error(
                f"[Batch] Error generating description for file {file_id}: {e}")
            _mark_file_failed(file_id, e)

    if not described_files:
        logger.info("[Batch] No files with descriptions to embed.")
        return

    # Phase 2: 批量 embedding
    texts = [f"文件名: {f.name}\n{desc}" for f, desc in described_files]
    logger.info(f"[Batch] Sending {len(texts)} texts for batch embedding...")

    vectors = file_service.batch_embedding_desc(texts, emb_config)
    logger.info(f"[Batch] Received {len(vectors)} embedding vectors.")

    # Phase 3: 写回数据库
    for (file, _), vector in zip(described_files, vectors):
        try:
            file.vector_info = vector
            file.status = "success"
            db.session.commit()
            logger.info(
                f"[Batch] Finished indexing file ID: {file.id} successfully.")
        except Exception as e:
            logger.error(
                f"[Batch] Error saving vector for file {file.id}: {e}")
            _mark_file_failed(file.id, e)


# 为了向后兼容，保留旧函数名的别名
handle_file_process = handle_file_indexing
