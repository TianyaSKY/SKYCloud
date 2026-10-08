"""Synchronous file-indexing worker handlers.

The worker deliberately keeps the whole file pipeline synchronous.  Network
calls, local parsing, and database writes may block one worker thread, while
the worker runtime provides the process-level concurrency with a thread pool.
Database sessions are opened only for short state transitions and writes; no
session is held while a file is downloaded or an LLM is running.
"""

from __future__ import annotations

from app.infra.datetime_utils import utc_now, BEIJING_TZ

import datetime
import os
import math
from dataclasses import dataclass

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
from app.infra.llm.sync_client import embed_texts
from app.features.folder.organize.description import (
    extract_file_sections,
    generate_file_description,
)
from app.infra.indexing.chunking import TextSection, build_text_chunks

from loguru import logger


@dataclass(frozen=True)
class FileIndexContext:
    """Immutable file metadata needed after the initial DB transaction."""

    file_id: int
    workspace_id: int
    uploader_id: int
    name: str
    object_name: str
    suffix: str


@dataclass(frozen=True)
class ChunkIndexResult:
    chunk_index: int
    page_number: int | None
    content: str
    vector: list[float]


@dataclass(frozen=True)
class FileIndexResult:
    file_id: int
    workspace_id: int
    name: str
    description: str
    vector: list[float]
    chunks: list[ChunkIndexResult]


def load_file_context(file_id: int) -> FileIndexContext | None:
    """Load metadata and mark the file processing in a short transaction."""
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
            suffix=os.path.splitext(str(file.name))[-1] or "",
        )
    finally:
        session.close()


def _save_file_description(file_id: int, description: str) -> None:
    """Persist a generated description without holding a session over the LLM call."""
    session = SessionLocal()
    try:
        file = session.get(File, file_id)
        if file is None:
            raise ResourceNotFoundError(f"File {file_id} not found")
        file.description = description
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _mark_file_failed(session, file_id: int, error: Exception) -> None:
    """Mark a file failed and notify its uploader using the supplied session."""
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
        logger.error("Failed to mark file {} as failed (error_type={})", file_id, type(inner_error).__name__)
        session.rollback()
        raise


def mark_file_failed(file_id: int, error: Exception) -> None:
    """Open a short-lived session for failure status and notification."""
    session = None
    try:
        session = SessionLocal()
        _mark_file_failed(session, file_id, error)
    except Exception as exc:
        logger.error("Could not persist failure status for file {} (error_type={})", file_id, type(exc).__name__)
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
    """Generate a complete searchable file vector or fail the indexing task."""
    return _embed_texts([text], config, user_id=user_id)[0]


def _embed_texts(
    texts: list[str],
    config: dict[str, str],
    *,
    user_id: int,
) -> list[list[float]]:
    """Require one valid database-compatible vector for every requested text."""
    if not texts:
        return []
    vectors = embed_texts(
        texts=texts,
        config=config,
        user_id=user_id,
        query_summary=f"batch({len(texts)} texts)",
    )
    if len(vectors) != len(texts):
        raise ValueError("Embedding response does not match the requested text count")
    for vector in vectors:
        if (len(vector) != 1024
                or any(not math.isfinite(value) for value in vector)
                or not any(value != 0 for value in vector)):
            raise ValueError("Embedding response must contain finite nonzero 1024-dimensional vectors")
    return vectors


def _build_chunk_results(
    *,
    file_name: str,
    local_path: str,
    description: str,
    emb_config: dict[str, str],
    user_id: int,
) -> list[ChunkIndexResult]:
    """Extract and embed chunks without touching the database."""
    sections = extract_file_sections(local_path)
    if not sections:
        # Images, videos, and scanned documents still need one searchable chunk.
        sections = [TextSection(description)]

    chunks = build_text_chunks(sections)
    if not chunks:
        raise ValueError("未能从文件中提取可索引内容")

    embedding_texts = [
        f"文件名: {file_name}\n内容:\n{chunk.content}" for chunk in chunks
    ]
    vectors = _embed_texts(embedding_texts, emb_config, user_id=user_id)
    if len(vectors) != len(chunks):
        raise ValueError("Embedding response does not match the extracted chunk count")

    return [
        ChunkIndexResult(
            chunk_index=chunk.chunk_index,
            page_number=chunk.page_number,
            content=chunk.content,
            vector=vector,
        )
        for chunk, vector in zip(chunks, vectors)
    ]


def _build_index_result(
    context: FileIndexContext,
    local_path: str,
    description: str,
    emb_config: dict[str, str],
) -> FileIndexResult:
    """Build all CPU/network indexing output without an ORM session."""
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
    """Write a complete index result in one short transaction."""
    session = SessionLocal()
    try:
        file = (
            session.query(File)
            .filter(File.id == result.file_id)
            .with_for_update()
            .one_or_none()
        )
        if file is None:
            raise ResourceNotFoundError(f"File {result.file_id} not found")

        file.description = result.description
        file.vector_info = result.vector or None
        session.query(FileChunk).filter(FileChunk.file_id == result.file_id).delete(
            synchronize_session=False
        )
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
            ]
        )
        file.status = "success"
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _replace_file_chunks(
    session,
    file: File,
    local_path: str,
    description: str,
    emb_config: dict,
    user_id: int,
) -> int:
    """Compatibility helper for callers that already own a DB session."""
    chunk_results = _build_chunk_results(
        file_name=str(file.name),
        local_path=local_path,
        description=description,
        emb_config=emb_config,
        user_id=user_id,
    )
    session.query(FileChunk).filter(FileChunk.file_id == file.id).delete(
        synchronize_session=False
    )
    session.add_all(
        [
            FileChunk(
                file_id=file.id,
                workspace_id=file.workspace_id,
                chunk_index=chunk.chunk_index,
                page_number=chunk.page_number,
                content=chunk.content,
                vector_info=chunk.vector or None,
            )
            for chunk in chunk_results
        ]
    )
    return len(chunk_results)


def _download_file(context: FileIndexContext) -> str:
    storage = get_storage_client()
    return storage.download_to_temp(context.object_name, suffix=context.suffix)


def handle_file_indexing(file_id: int) -> None:
    """Index one file using a synchronous, session-bounded pipeline."""
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
        logger.error("Error indexing file {} (error_type={})", file_id, type(exc).__name__)
        if context is not None:
            try:
                mark_file_failed(file_id, exc)
            except Exception as persist_error:
                logger.error("Could not persist failure status for file {} (error_type={})", file_id, type(persist_error).__name__)
                raise
        else:
            raise
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)


# Compatibility import name used by the worker entry point and older callers.
handle_file_process = handle_file_indexing
