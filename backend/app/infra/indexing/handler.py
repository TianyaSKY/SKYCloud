"""同步文件索引 worker 处理器.

Worker 刻意保持整个文件处理流水线为同步执行。网络
调用、本地解析和数据库写入可能会阻塞单个 worker 线程, 而
worker 运行时通过线程池提供进程级并发。
数据库会话仅在简短的状态转换和写入时打开; 在下载文件
或运行大语言模型期间不持有任何会话。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from app.exceptions import ResourceNotFoundError
from app.features.file import service as file_service
from app.features.folder.organize.description import (
    extract_file_sections,
    generate_file_description,
)
from app.features.inbox import service as inbox_service
from app.infra.datetime_utils import BEIJING_TZ, utc_now
from app.infra.extensions import SessionLocal
from app.infra.indexing.chunking import TextSection, build_text_chunks
from app.infra.llm.config import (
    get_chat_model_config,
    get_embedding_model_config,
    get_vl_model_config,
)
from app.infra.llm.sync_client import embed_texts
from app.infra.storage import get_storage_client
from app.models.file import File
from app.models.file_chunk import FileChunk

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

EMBEDDING_DIMENSIONS = 1024


@dataclass(frozen=True)
class FileIndexContext:
    """在初始数据库事务后所需的不可变文件元数据."""

    file_id: int
    workspace_id: int
    uploader_id: int
    name: str
    object_name: str
    suffix: str


@dataclass(frozen=True)
class ChunkIndexResult:
    """为单个文件分块准备的向量载荷."""

    chunk_index: int
    page_number: int | None
    content: str
    vector: list[float]


@dataclass(frozen=True)
class FileIndexResult:
    """在最终单个事务中写入的完整索引结果."""

    file_id: int
    workspace_id: int
    name: str
    description: str
    vector: list[float]
    chunks: list[ChunkIndexResult]


def _require_file(session: Session, file_id: int) -> File:
    """加载单个文件记录, 若不存在则抛出统一的资源未找到异常."""
    file = session.get(File, file_id)
    if file is None:
        message = f"File {file_id} not found"
        raise ResourceNotFoundError(message)
    return file


def _require_locked_file(session: Session, file_id: int) -> File:
    """锁定单个文件行, 若不存在则抛出统一的资源未找到异常."""
    file = (
        session.query(File)
        .filter(File.id == file_id)
        .with_for_update()
        .one_or_none()
    )
    if file is None:
        message = f"File {file_id} not found"
        raise ResourceNotFoundError(message)
    return file


def load_file_context(file_id: int) -> FileIndexContext | None:
    """在简短事务中加载元数据并将文件状态标记为处理中."""
    session = SessionLocal()
    try:
        try:
            file: File = file_service.get_file(session, file_id)
        except ResourceNotFoundError:
            logger.error("File ID {} not found.", file_id)
            return None

        file.status = "processing"
        session.commit()
        return FileIndexContext(
            file_id=int(file.id),
            workspace_id=int(file.workspace_id),
            uploader_id=int(file.uploader_id or 0),
            name=str(file.name),
            object_name=str(file.file_path),
            suffix=Path(str(file.name)).suffix,
        )
    finally:
        session.close()


def _save_file_description(file_id: int, description: str) -> None:
    """持久化保存生成的描述信息, 不在 LLM 调用期间持有会话."""
    session = SessionLocal()
    try:
        file = _require_file(session, file_id)
        file.description = description
        session.commit()
    except Exception:
        # 重新抛出异常前, 必须对任何 ORM/驱动层失败执行回滚。
        session.rollback()
        raise
    finally:
        session.close()


def _mark_file_failed(
    session: Session,
    file_id: int,
    _error: BaseException,
) -> None:
    """使用传入的会话将文件标记为失败并通知上传者."""
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
                        f"时间:{utc_now().astimezone(BEIJING_TZ).isoformat()}\n"
                        f"文件id:{file_id}\n"
                        "请稍后重试或联系管理员。\n"
                    ),
                },
            )
    except Exception as inner_error:
        logger.error(
            "Failed to mark file {} as failed (error_type={})",
            file_id,
            type(inner_error).__name__,
        )
        session.rollback()
        raise


def mark_file_failed(file_id: int, _error: BaseException) -> None:
    """开启短期会话以记录失败状态并发送通知."""
    session: Session | None = None
    try:
        session = SessionLocal()
        _mark_file_failed(session, file_id, _error)
    except Exception as exc:
        logger.error(
            "Could not persist failure status for file {} (error_type={})",
            file_id,
            type(exc).__name__,
        )
        raise
    finally:
        if session is not None:
            session.close()


def _embed_text(
    text: str,
    config: dict[str, str],
    *,
    user_id: int,
) -> list[float]:
    """生成完整的可检索文件向量, 否则判定索引任务失败."""
    return _embed_texts([text], config, user_id=user_id)[0]


def _embed_texts(
    texts: list[str],
    config: dict[str, str],
    *,
    user_id: int,
) -> list[list[float]]:
    """对每个请求的文本必须生成有效且兼容数据库的向量."""
    if not texts:
        return []
    vectors = embed_texts(
        texts=texts,
        config=config,
        user_id=user_id,
        query_summary=f"batch({len(texts)} texts)",
    )
    if len(vectors) != len(texts):
        message = "Embedding response does not match the requested text count"
        raise ValueError(message)
    for vector in vectors:
        if (
            len(vector) != EMBEDDING_DIMENSIONS
            or any(not math.isfinite(value) for value in vector)
            or not any(value != 0 for value in vector)
        ):
            message = (
                "Embedding response must contain finite nonzero"
                f" {EMBEDDING_DIMENSIONS}-dimensional vectors"
            )
            raise ValueError(message)
    return vectors


def _build_chunk_results(
    *,
    file_name: str,
    local_path: str,
    description: str,
    emb_config: dict[str, str],
    user_id: int,
) -> list[ChunkIndexResult]:
    """在不触碰数据库的前提下提取并嵌入分块."""
    sections = extract_file_sections(local_path)
    if not sections:
        # 图片、视频和扫描文档仍需要一个可检索的分块。
        sections = [TextSection(description)]

    chunks = build_text_chunks(sections)
    if not chunks:
        message = "未能从文件中提取可索引内容"
        raise ValueError(message)

    embedding_texts = [
        f"文件名: {file_name}\n内容:\n{chunk.content}" for chunk in chunks
    ]
    vectors = _embed_texts(embedding_texts, emb_config, user_id=user_id)
    if len(vectors) != len(chunks):
        message = "Embedding response does not match the extracted chunk count"
        raise ValueError(message)

    return [
        ChunkIndexResult(
            chunk_index=chunk.chunk_index,
            page_number=chunk.page_number,
            content=chunk.content,
            vector=vector,
        )
        for chunk, vector in zip(chunks, vectors, strict=False)
    ]


def _build_index_result(
    context: FileIndexContext,
    local_path: str,
    description: str,
    emb_config: dict[str, str],
) -> FileIndexResult:
    """在不使用 ORM 会话的情况下构建所有 CPU/网络索引产物."""
    file_vector = _embed_text(
        f"文件名: {context.name}\n{description}",
        emb_config,
        user_id=context.uploader_id,
    )

    return FileIndexResult(
        file_id=context.file_id,
        workspace_id=context.workspace_id,
        name=context.name,
        description=description,
        vector=file_vector,
        chunks=_build_chunk_results(
            file_name=context.name,
            local_path=local_path,
            description=description,
            emb_config=emb_config,
            user_id=context.uploader_id,
        ),
    )


def _save_index_result(result: FileIndexResult) -> None:
    """在单个简短事务中写入完整的索引结果."""
    session = SessionLocal()
    try:
        file = _require_locked_file(session, result.file_id)

        file.description = result.description
        file.vector_info = result.vector or None
        session.query(FileChunk).filter(
            FileChunk.file_id == result.file_id,
        ).delete(synchronize_session=False)
        session.add_all(
            [
                FileChunk(
                    file_id=result.file_id,
                    workspace_id=result.workspace_id,
                    chunk_index=chunk.chunk_index,
                    page_number=chunk.page_number,
                    content=chunk.content,
                    vector_info=chunk.vector or None,
                )
                for chunk in result.chunks
            ],
        )
        file.status = "success"
        session.commit()
    except Exception:
        # 重新抛出异常前, 必须对任何 ORM/驱动层失败执行回滚。
        session.rollback()
        raise
    finally:
        session.close()


def _download_file(context: FileIndexContext) -> str:
    """将对象文件获取到临时路径以供本地解析."""
    storage = get_storage_client()
    return storage.download_to_temp(context.object_name, suffix=context.suffix)


def handle_file_indexing(file_id: int) -> None:
    """使用同步且受会话边界保护的流水线对单个文件建立索引."""
    context: FileIndexContext | None = None
    local_path: str | None = None
    try:
        context = load_file_context(file_id)
        if context is None:
            return

        vl_config = get_vl_model_config()
        chat_config = get_chat_model_config()
        emb_config = get_embedding_model_config()

        local_path = _download_file(context)
        description = generate_file_description(
            local_path,
            vl_config,
            chat_config,
            user_id=context.uploader_id,
        )
        _save_file_description(context.file_id, description)

        result = _build_index_result(
            context,
            local_path,
            description,
            emb_config,
        )
        _save_index_result(result)
        logger.info(
            "Finished indexing file ID %s successfully with %s chunks.",
            file_id,
            len(result.chunks),
        )
    except Exception as exc:
        # 单个文件失败不得中断 worker 任务; 失败状态已持久化,
        # 原始异常仍会向上传播给任务所有者。
        logger.error(
            "Error indexing file {} (error_type={})",
            file_id,
            type(exc).__name__,
        )
        if context is not None:
            try:
                mark_file_failed(file_id, exc)
            except Exception as persist_error:
                logger.error(
                    "Could not persist failure status for file {} (error_type={})",
                    file_id,
                    type(persist_error).__name__,
                )
                raise
        else:
            raise
    finally:
        if local_path is not None:
            Path(local_path).unlink(missing_ok=True)


