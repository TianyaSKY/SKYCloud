"""Multipart upload orchestration over quota, file publication and byte adapters.

The caller owns the SQL Session. Reservation locks remain held across chunk
writes and final publication; compensation preserves existing commit semantics.
"""
from dataclasses import dataclass
import math
import os
import re
import uuid
from typing import Any, Callable, cast

from loguru import logger
from sqlalchemy.orm import Session
from app.exceptions import BusinessRuleError, ConflictError, DomainError, PayloadTooLargeError, ServiceOperationError
from app.features.file import repository, upload_authorization, upload_quota
from app.features.file.upload_recovery import compensate_failed_uploads
from app.infra import multipart_storage
from app.infra.multipart_cleanup import remove_multipart_upload_safely
from app.infra.file_task_outbox import stage_file_tasks
from app.infra.upload_staging import StagedUpload, close_staging_safely
from app.models.file import File


@dataclass(frozen=True)
class MultipartSettings:
    upload_root: str
    multipart_root: str
    default_chunk_size: int
    max_chunk_size: int
    max_upload_size: int
    max_total_chunks: int


@dataclass(frozen=True)
class MultipartFileOperations:
    """Shared file admission/publication contracts, independent of HTTP routes."""
    resolve_parent: Callable[[Session, int, int | None], int | None]
    normalize_hash: Callable[[str | None], str | None]
    reusable_source: Callable[[Session, str, int, int], File | None]
    clone_source: Callable[..., File]
    object_name: Callable[[str], str]
    hash_file: Callable[[str], str]
    mime_type: Callable[[str, str | None], str | None]
    created: Callable[[File], None]
    invalidate: Callable[[list[int], int], None]


def validate_upload_id(upload_id: str) -> str:
    candidate = (upload_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", candidate):
        raise BusinessRuleError("Invalid upload_id")
    return candidate


class MultipartUploadService:
    def __init__(self, settings: MultipartSettings, files: MultipartFileOperations, *,
                 storage_factory: Callable, write_metadata: Callable = multipart_storage.write_metadata):
        self.settings = settings
        self.files = files
        self.storage_factory = storage_factory
        self.write_metadata = write_metadata

    def directory(self, workspace_id: int, upload_id: str) -> str:
        return multipart_storage.upload_directory(self.settings.multipart_root, workspace_id, upload_id)

    def init_multipart_upload(self, session: Session, workspace_id: int, uploader_id: int, data: dict[str, Any]) -> dict[str, Any]:
        filename = (data.get("filename") or "").strip()
        if not filename:
            raise BusinessRuleError("filename is required")

        total_size = int(data.get("total_size") or 0)
        if total_size <= 0:
            raise BusinessRuleError("total_size must be positive")
        if 0 < self.settings.max_upload_size < total_size:
            raise PayloadTooLargeError("File too large")

        chunk_size = int(data.get("chunk_size") or self.settings.default_chunk_size)
        if chunk_size <= 0:
            raise BusinessRuleError("chunk_size must be positive")
        if chunk_size > self.settings.max_chunk_size:
            raise BusinessRuleError("chunk_size exceeds server limit")

        total_chunks = math.ceil(total_size / chunk_size)
        if total_chunks <= 0 or total_chunks > self.settings.max_total_chunks:
            raise BusinessRuleError("total_chunks exceeds server limit")

        parent_id = self.files.resolve_parent(session, workspace_id, data.get("parent_id"))
        mime_type = data.get("mime_type")
        content_hash = self.files.normalize_hash(data.get("content_hash"))

        if content_hash:
            source_file = self.files.reusable_source(session, content_hash, total_size, workspace_id)
            if source_file:
                new_file = self.files.clone_source(session,
                    source_file,
                    filename=filename,
                    workspace_id=workspace_id,
                    uploader_id=uploader_id,
                    parent_id=parent_id,
                    mime_type=mime_type,
                    content_hash=content_hash,
                )
                return {
                    "upload_id": None,
                    "chunk_size": chunk_size,
                    "total_chunks": total_chunks,
                    "uploaded_chunks": [],
                    "instant_upload": True,
                    "file": new_file.to_dict(),
                }

        upload_id = data.get("upload_id") or uuid.uuid4().hex
        upload_id = validate_upload_id(upload_id)

        upload_dir = self.directory(workspace_id, upload_id)
        chunks_dir = multipart_storage.chunks_directory(upload_dir)
        meta_path = multipart_storage.metadata_path(upload_dir)

        existing = upload_quota.find_reservation(session, upload_id)
        metadata = {
            "upload_id": upload_id, "workspace_id": workspace_id, "uploader_id": uploader_id,
            "filename": filename, "total_size": total_size, "chunk_size": chunk_size,
            "total_chunks": total_chunks, "parent_id": parent_id, "mime_type": mime_type,
            "content_hash": content_hash,
            "object_name": existing.details.get("object_name") if existing else self.files.object_name(filename),
            "stage_id": existing.details.get("stage_id") if existing else uuid.uuid4().hex,
        }
        if existing is not None:
            reservation = upload_quota.owned(session, workspace_id, uploader_id, upload_id)
            if reservation.kind != "multipart" or reservation.details != metadata:
                raise ConflictError("upload_id already exists with different file metadata")
            meta = reservation.details
            session.commit()
        else:
            reservation = upload_authorization.reserve(session, workspace_id, uploader_id, total_size,
                                              kind="multipart", reservation_id=upload_id, details=metadata)
            meta = metadata
            try:
                multipart_storage.prepare_chunks(chunks_dir, root=self.settings.multipart_root)
                self.write_metadata(meta_path, meta, root=self.settings.multipart_root)
            except Exception:
                compensate_failed_uploads(session, workspace_id, uploader_id,
                                          [(upload_id, metadata["object_name"])], None)
                remove_multipart_upload_safely(self.settings.multipart_root, workspace_id, upload_id)
                raise ServiceOperationError("Failed to initialize upload")

        return {
            "upload_id": upload_id,
            "chunk_size": int(meta["chunk_size"]),
            "total_chunks": int(meta["total_chunks"]),
            "uploaded_chunks": multipart_storage.list_chunks(chunks_dir, root=self.settings.multipart_root),
            "instant_upload": False,
        }


    def get_multipart_upload_status(self, session: Session, workspace_id: int, uploader_id: int, upload_id: str) -> dict[str, Any]:
        meta = upload_quota.owned(session, workspace_id, uploader_id, upload_id).details
        session.commit()
        upload_dir = self.directory(workspace_id, validate_upload_id(upload_id))
        chunks_dir = multipart_storage.chunks_directory(upload_dir)
        return {
            "upload_id": meta["upload_id"],
            "chunk_size": int(meta["chunk_size"]),
            "total_chunks": int(meta["total_chunks"]),
            "uploaded_chunks": multipart_storage.list_chunks(chunks_dir, root=self.settings.multipart_root),
        }


    def save_multipart_chunk(self,
            session: Session,
            uploader_id: int,
            workspace_id: int,
            upload_id: str,
            chunk_index: int,
            chunk_obj: Any,
    ) -> dict[str, Any]:
        safe_upload_id = validate_upload_id(upload_id)
        meta = upload_quota.owned(session, workspace_id, uploader_id, safe_upload_id).details

        try:
            idx = int(chunk_index)
        except (TypeError, ValueError):
            raise BusinessRuleError("Invalid chunk_index")

        total_chunks = int(meta["total_chunks"])
        chunk_size = int(meta["chunk_size"])
        total_size = int(meta["total_size"])

        if idx < 0 or idx >= total_chunks:
            raise BusinessRuleError("chunk_index out of range")

        upload_dir = self.directory(workspace_id, safe_upload_id)
        chunks_dir = multipart_storage.chunks_directory(upload_dir)
        expected_size = chunk_size
        if idx == total_chunks - 1:
            expected_size = total_size - (idx * chunk_size)
            if expected_size <= 0:
                expected_size = chunk_size

        multipart_storage.save_chunk(chunks_dir, idx, chunk_obj, expected_size, self.settings.max_chunk_size,
                                     root=self.settings.multipart_root)
        session.commit()

        return {
            "upload_id": safe_upload_id,
            "chunk_index": idx,
            "uploaded_chunks": multipart_storage.list_chunks(chunks_dir, root=self.settings.multipart_root),
        }


    def complete_multipart_upload(self, session: Session, workspace_id: int, uploader_id: int, upload_id: str) -> File:
        safe_upload_id = validate_upload_id(upload_id)
        meta = upload_quota.owned(session, workspace_id, uploader_id, safe_upload_id).details
        if not meta.get("object_name") or not meta.get("stage_id"):
            raise ConflictError("Upload session must be reinitialized after upgrade")

        total_chunks = int(meta["total_chunks"])
        total_size = int(meta["total_size"])
        if total_size <= 0 or total_size > self.settings.max_upload_size:
            raise PayloadTooLargeError("File too large")
        filename = meta["filename"]
        parent_id = self.files.resolve_parent(session, workspace_id, meta.get("parent_id"))
        mime_type = meta.get("mime_type")
        content_hash = self.files.normalize_hash(meta.get("content_hash"))

        upload_dir = self.directory(workspace_id, safe_upload_id)
        chunks_dir = multipart_storage.chunks_directory(upload_dir)

        uploaded_chunks = set(multipart_storage.list_chunks(chunks_dir, root=self.settings.multipart_root))
        missing = [idx for idx in range(total_chunks) if idx not in uploaded_chunks]
        if missing:
            preview = ",".join(str(i) for i in missing[:10])
            raise BusinessRuleError(f"Missing chunks: {preview}")

        os.makedirs(self.settings.upload_root, exist_ok=True)
        unique_filename = meta["object_name"]
        staging = StagedUpload(self.settings.upload_root, filename, stage_id=meta["stage_id"])
        final_path = staging.path
        try:
            multipart_storage.assemble(chunks_dir, final_path, total_chunks=total_chunks,
                                       chunk_size=int(meta["chunk_size"]), total_size=total_size,
                                       root=self.settings.multipart_root)
        except DomainError:
            close_staging_safely(staging)
            raise
        except Exception as e:
            close_staging_safely(staging)
            logger.error("Failed to merge chunks (error_type={})", type(e).__name__)
            raise ServiceOperationError("Failed to merge chunks")
        except BaseException:
            close_staging_safely(staging)
            raise

        storage = None
        try:
            # 上传合并后的文件到 MinIO
            actual_hash = self.files.hash_file(final_path)
            if content_hash and actual_hash != content_hash:
                raise BusinessRuleError("Uploaded content does not match its hash")
            storage = self.storage_factory()
            storage.upload_file(final_path, unique_filename)

            resolved_mime = self.files.mime_type(final_path, mime_type)
            new_file = File(
                name=filename,
                file_path=unique_filename,
                file_size=total_size,
                mime_type=resolved_mime,
                content_hash=actual_hash,
                workspace_id=workspace_id,
                uploader_id=uploader_id,
                parent_id=parent_id,
            )
            upload_authorization.consume(session, workspace_id, uploader_id, safe_upload_id, total_size)
            new_file.parent_id = self.files.resolve_parent(session, workspace_id, parent_id)
            repository.insert_file(session, new_file)
            stage_file_tasks(session, [new_file.id])
            session.commit()
        except Exception as e:
            compensate_failed_uploads(session, workspace_id, uploader_id,
                                      [(safe_upload_id, unique_filename)], storage)
            logger.error("Failed to persist merged file (error_type={})", type(e).__name__)
            if isinstance(e, DomainError):
                raise
            raise ServiceOperationError("Failed to save file metadata")
        finally:
            # 清理本地合并文件与分片目录
            close_staging_safely(staging)
            remove_multipart_upload_safely(self.settings.multipart_root, workspace_id, safe_upload_id)

        self.files.created(new_file)

        self.files.invalidate([cast(int, new_file.id)], workspace_id)
        return new_file


    def abort_multipart_upload(self, session: Session, workspace_id: int, uploader_id: int, upload_id: str) -> None:
        safe_upload_id = validate_upload_id(upload_id)
        upload_quota.cancel(session, workspace_id, uploader_id, safe_upload_id)
        remove_multipart_upload_safely(self.settings.multipart_root, workspace_id, safe_upload_id)
