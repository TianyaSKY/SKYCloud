"""文件索引 Worker：描述生成 + embedding 写库，供语义检索使用。

由 RabbitMQ 消费者调用；失败标记 status=fail 并写收件箱通知。
单文件与批量路径共享失败收尾逻辑，避免连接池上的半事务。
文件从 MinIO 对象存储下载到临时目录后交给描述生成模块处理。
"""

import datetime
import logging
import os
from typing import cast

from app.exceptions import ResourceNotFoundError
from app.infra.extensions import SessionLocal
from app.infra.storage import get_storage_client
from app.models.file import File
from app.models.file_chunk import FileChunk
from app.features.file import service as file_service
from app.features.inbox import service as inbox_service
from app.infra.llm.config import (
    get_chat_model_config,
    get_embedding_model_config,
    get_vl_model_config,
)
from app.features.folder.organize.description import (
    extract_file_sections,
    generate_file_description,
)
from app.infra.indexing.chunking import TextSection, build_text_chunks

logger = logging.getLogger(__name__)


def _replace_file_chunks(
        session, file: File, local_path: str, description: str, emb_config: dict, user_id: int
) -> int:
    """用最新文件内容重建 chunk 向量，旧 chunk 在同一事务中替换。"""
    sections = extract_file_sections(local_path)
    if not sections:
        # 图像、视频或扫描件没有可抽取正文时，保留 AI 描述作为唯一可检索片段。
        sections = [TextSection(description)]
    chunks = build_text_chunks(sections)
    if not chunks:
        raise ValueError("未能从文件中提取可索引内容")

    embedding_texts = [f"文件名: {file.name}\n内容:\n{chunk.content}" for chunk in chunks]
    vectors = file_service.batch_embedding_desc(embedding_texts, emb_config, user_id=user_id)
    if len(vectors) != len(chunks):
        raise RuntimeError("Embedding 返回数量与分块数量不一致")

    session.query(FileChunk).filter(FileChunk.file_id == file.id).delete(
        synchronize_session=False
    )
    session.add_all([
        FileChunk(
            file_id=file.id,
            workspace_id=file.workspace_id,
            chunk_index=chunk.chunk_index,
            page_number=chunk.page_number,
            content=chunk.content,
            vector_info=vector or None,
        )
        for chunk, vector in zip(chunks, vectors)
    ])
    return len(chunks)


def handle_file_indexing(file_id: int) -> None:
    """索引单个文件：processing → 描述 → 向量 → success；异常则 fail + 通知。"""
    session = SessionLocal()
    tmp_path = None
    try:
        try:
            file: File = file_service.get_file(session, file_id)
        except ResourceNotFoundError:
            logger.error(f"File ID {file_id} not found.")
            return

        logger.info(f"Starting to index file: {file.name} (ID: {file_id})")

        file.status = "processing"
        session.commit()

        vl_config = get_vl_model_config()
        chat_config = get_chat_model_config()
        emb_config = get_embedding_model_config()

        # 从 MinIO 下载到临时文件
        storage = get_storage_client()
        object_name = str(file.file_path)
        suffix = os.path.splitext(str(file.name))[-1] or ""
        tmp_path = storage.download_to_temp(object_name, suffix=suffix)

        # 文本走 Chat，其它走 VL
        description = generate_file_description(
            tmp_path, vl_config, chat_config, user_id=file.uploader_id or 0)
        file.description = description
        session.commit()

        # 文件名拼进 embedding 文本，使纯文件名查询也能够命中
        embedding_text = f"文件名: {file.name}\n{description}"
        file.vector_info = file_service.embedding_desc(
            embedding_text, emb_config, user_id=file.uploader_id or 0)
        chunk_count = _replace_file_chunks(
            session, file, tmp_path, description, emb_config, file.uploader_id or 0
        )

        file.status = "success"
        session.commit()
        logger.info(f"Finished indexing file ID: {file_id} successfully with {chunk_count} chunks.")

    except Exception as e:
        logger.error(f"Error indexing file {file_id}: {e}")
        session.rollback()

        file = session.get(File, file_id)
        if file:
            file.status = "fail"
            session.commit()

            inbox_service.create_inbox_message(
                session,
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
                },
            )
    finally:
        # 清理临时文件
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        session.close()


def _mark_file_failed(session, file_id: int, error: Exception) -> None:
    """批量路径专用：回滚后标 fail 并通知，内层异常不再上抛。"""
    try:
        session.rollback()
        file = session.get(File, file_id)
        if file:
            file.status = "fail"
            session.commit()

            inbox_service.create_inbox_message(
                session,
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
                },
            )
    except Exception as inner_e:
        logger.error(f"Failed to mark file {file_id} as failed: {inner_e}")
        session.rollback()


def handle_batch_indexing(file_ids: list[int]) -> None:
    """批量索引：描述仍逐文件（VL 难批），embedding 一次 batch 调用降延迟。"""
    if not file_ids:
        return

    vl_config = get_vl_model_config()
    chat_config = get_chat_model_config()
    emb_config = get_embedding_model_config()
    storage = get_storage_client()

    session = SessionLocal()
    tmp_paths: list[str] = []  # 记录临时文件以便统一清理
    try:
        # 阶段 1：逐个生成描述（VL 难批量）
        described_files: list[tuple[File, str]] = []
        for file_id in file_ids:
            try:
                file: File = file_service.get_file(session, file_id)

                logger.info(
                    f"[Batch] Starting description for: {file.name} (ID: {file_id})")
                file.status = "processing"
                session.commit()

                # 从 MinIO 下载到临时文件
                object_name = str(file.file_path)
                suffix = os.path.splitext(str(file.name))[-1] or ""
                tmp_path = storage.download_to_temp(object_name, suffix=suffix)
                tmp_paths.append(tmp_path)

                description = generate_file_description(
                    tmp_path, vl_config, chat_config, user_id=file.uploader_id or 0)
                file.description = description
                session.commit()

                described_files.append((file, description))
                logger.info(
                    f"[Batch] Description generated for file ID: {file_id}")

            except ResourceNotFoundError:
                logger.error(f"File ID {file_id} not found.")
            except Exception as e:
                logger.error(
                    f"[Batch] Error generating description for file {file_id}: {e}")
                _mark_file_failed(session, file_id, e)

        if not described_files:
            logger.info("[Batch] No files with descriptions to embed.")
            return

        # 阶段 2：批量 embedding，降低往返次数
        texts = [f"文件名: {f.name}\n{desc}" for f, desc in described_files]
        logger.info(f"[Batch] Sending {len(texts)} texts for batch embedding...")

        # Token 记账挂到批次首文件上传者
        batch_user_id = described_files[0][0].uploader_id or 0 if described_files else 0
        vectors = file_service.batch_embedding_desc(texts, emb_config, user_id=batch_user_id)
        logger.info(f"[Batch] Received {len(vectors)} embedding vectors.")

        # 阶段 3：逐文件写回向量与状态
        for (file, _), vector in zip(described_files, vectors):
            try:
                file.vector_info = vector
                object_name = str(file.file_path)
                suffix = os.path.splitext(str(file.name))[-1] or ""
                chunk_tmp_path = storage.download_to_temp(object_name, suffix=suffix)
                tmp_paths.append(chunk_tmp_path)
                chunk_count = _replace_file_chunks(
                    session, file, chunk_tmp_path, cast(str, file.description),
                    emb_config, file.uploader_id or 0,
                )
                file.status = "success"
                session.commit()
                logger.info(
                    f"[Batch] Finished indexing file ID: {file.id} successfully with {chunk_count} chunks.")
            except Exception as e:
                logger.error(
                    f"[Batch] Error saving vector for file {file.id}: {e}")
                _mark_file_failed(session, file.id, e)
    finally:
        # 清理所有临时文件
        for p in tmp_paths:
            if os.path.exists(p):
                os.remove(p)
        session.close()


# 兼容旧 import 名
handle_file_process = handle_file_indexing
