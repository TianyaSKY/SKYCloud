"""Synchronous file-indexing worker handlers.

The worker deliberately keeps the whole file pipeline synchronous.  Network
calls, local parsing, and database writes may block one worker thread, while
the worker runtime provides the process-level concurrency with a thread pool.
Database sessions are opened only for short state transitions and writes; no
session is held while a file is downloaded or an LLM is running.
"""

from __future__ import annotations

import datetime
import logging
import os
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

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class PreparedFile:
    context: FileIndexContext
    local_path: str
    description: str


def load_file_context(file_id: int) -> FileIndexContext | None:
    """Load metadata and mark the file processing in a short transaction."""
    session = SessionLocal()
    try:
        try:
            file: File = file_service.get_file(session, file_id)
        except ResourceNotFoundError:
            logger.error("File ID %s not found.", file_id)
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
                        f"时间:{datetime.datetime.now()}\n"
                        f"文件id:{file_id}\n"
                        f"{error}\n"
                    ),
                },
            )
    except Exception as inner_error:
        logger.error("Failed to mark file %s as failed: %s", file_id, inner_error)
        session.rollback()


def mark_file_failed(file_id: int, error: Exception) -> None:
    """Open a short-lived session for failure status and notification."""
    session = None
    try:
        session = SessionLocal()
        _mark_file_failed(session, file_id, error)
    except Exception as exc:
        logger.error("Could not persist failure status for file %s: %s", file_id, exc)
    finally:
        if session is not None:
            session.close()


def _embed_text(
    text: str,
    config: dict[str, str],
    *,
    user_id: int,
) -> list[float]:
    """Worker-compatible single embedding; failed calls degrade to an empty vector."""
    try:
        vectors = embed_texts(
            texts=text,
            config=config,
            user_id=user_id,
            query_summary=text[:100] if text else None,
        )
        return vectors[0] if vectors else []
    except Exception as exc:
        logger.exception("Embedding failed: %s", exc)
        return []


def _embed_texts(
    texts: list[str],
    config: dict[str, str],
    *,
    user_id: int,
) -> list[list[float]]:
    """Worker-compatible batch embedding that keeps result alignment on failure."""
    if not texts:
        return []
    try:
        return embed_texts(
            texts=texts,
            config=config,
            user_id=user_id,
            query_summary=f"batch({len(texts)} texts)",
        )
    except Exception as exc:
        logger.exception("Batch embedding failed: %s", exc)
        return [[] for _ in texts]


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
        logger.warning(
            "Embedding returned %s vectors for %s chunks; padding to preserve alignment",
            len(vectors),
            len(chunks),
        )
        vectors = vectors[: len(chunks)] + [
            [] for _ in range(len(chunks) - len(vectors))
        ]

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
    *,
    description_vector: list[float] | None = None,
) -> FileIndexResult:
    """Build all CPU/network indexing output without an ORM session."""
    file_vector = description_vector
    if file_vector is None:
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
        file = session.get(File, result.file_id)
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
        logger.exception("Error indexing file %s: %s", file_id, exc)
        if context is not None:
            try:
                mark_file_failed(file_id, exc)
            except Exception:
                logger.exception("Could not persist failure status for file %s", file_id)
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)


def handle_batch_indexing(file_ids: list[int]) -> None:
    """Index a batch synchronously, batching only the description embeddings."""
    if not file_ids:
        return

    vl_config = get_vl_model_config()
    chat_config = get_chat_model_config()
    emb_config = get_embedding_model_config()
    prepared: list[PreparedFile] = []

    try:
        for file_id in file_ids:
            context: FileIndexContext | None = None
            local_path: str | None = None
            try:
                context = load_file_context(file_id)
                if context is None:
                    continue

                local_path = _download_file(context)
                description = generate_file_description(
                    local_path,
                    vl_config,
                    chat_config,
                    user_id=context.uploader_id,
                )
                _save_file_description(context.file_id, description)
                prepared.append(PreparedFile(context, local_path, description))
                logger.info("[Batch] Description generated for file ID %s", file_id)
            except Exception as exc:
                logger.exception(
                    "[Batch] Error generating description for file %s: %s",
                    file_id,
                    exc,
                )
                mark_file_failed(file_id, exc)
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)

        if not prepared:
            logger.info("[Batch] No files with descriptions to embed.")
            return

        description_texts = [
            f"文件名: {item.context.name}\n{item.description}" for item in prepared
        ]
        batch_user_id = prepared[0].context.uploader_id
        description_vectors = _embed_texts(
            description_texts,
            emb_config,
            user_id=batch_user_id,
        )
        if len(description_vectors) != len(prepared):
            description_vectors = (
                description_vectors[: len(prepared)]
                + [[] for _ in range(len(prepared) - len(description_vectors))]
            )

        for item, description_vector in zip(prepared, description_vectors):
            try:
                result = _build_index_result(
                    item.context,
                    item.local_path,
                    item.description,
                    emb_config,
                    description_vector=description_vector,
                )
                _save_index_result(result)
                logger.info(
                    "[Batch] Finished indexing file ID %s successfully with %s chunks.",
                    item.context.file_id,
                    len(result.chunks),
                )
            except Exception as exc:
                logger.exception(
                    "[Batch] Error saving vector for file %s: %s",
                    item.context.file_id,
                    exc,
                )
                mark_file_failed(item.context.file_id, exc)
    finally:
        for item in prepared:
            if os.path.exists(item.local_path):
                os.remove(item.local_path)


# Compatibility import name used by the worker entry point and older callers.
handle_file_process = handle_file_indexing
