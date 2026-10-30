"""Multipart upload orchestration over quota, file publication and byte adapters.

The caller owns the SQL Session. Reservation locks remain held across chunk
writes and final publication; compensation preserves existing commit semantics.
"""

import math
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from loguru import logger
from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    ConflictError,
    DomainError,
    PayloadTooLargeError,
    ServiceOperationError,
)
from app.features.file import repository, upload_authorization, upload_quota
from app.features.file.upload_quota import CancelParams, ReserveParams
from app.features.file.upload_recovery import compensate_failed_uploads
from app.features.file.upload_service import CloneSourceParams, UploadSource
from app.infra import multipart_storage
from app.infra.file_task_outbox import stage_file_tasks
from app.infra.multipart_cleanup import remove_multipart_upload_safely
from app.infra.storage import MinIOStorageClient
from app.infra.upload_staging import StagedUpload, close_staging_safely
from app.models.file import File
from app.models.upload_reservation import UploadReservation


class MultipartInitData(TypedDict, total=False):
    """Multipart start inputs decoded from the request payload."""

    filename: str
    total_size: int
    chunk_size: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str | None
    upload_id: str


class MultipartMetadata(TypedDict):
    """Session metadata persisted in the reservation details column."""

    upload_id: str
    workspace_id: int
    uploader_id: int
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str | None
    object_name: str
    stage_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingMultipart:
    """Validated multipart session request ready to be persisted."""

    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int


@dataclass(frozen=True, slots=True, kw_only=True)
class MultipartPublication:
    """Owner and resolved metadata shared by every publication step."""

    workspace_id: int
    uploader_id: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SaveMultipartChunkParams:
    """Inputs for storing one uploaded chunk."""

    uploader_id: int
    workspace_id: int
    upload_id: str
    chunk_index: int
    chunk: UploadSource


@dataclass(frozen=True, slots=True)
class MultipartSettings:
    """Filesystem roots and chunk ceilings for multipart uploads."""

    upload_root: str
    multipart_root: str
    default_chunk_size: int
    max_chunk_size: int
    max_upload_size: int
    max_total_chunks: int


@dataclass(frozen=True, slots=True)
class MultipartFileOperations:
    """Shared file admission/publication contracts, independent of HTTP routes."""

    resolve_parent: Callable[[Session, int, int | None], int | None]
    normalize_hash: Callable[[str | None], str | None]
    reusable_source: Callable[[Session, str, int, int], File | None]
    clone_source: Callable[[Session, CloneSourceParams], File]
    object_name: Callable[[str], str]
    hash_file: Callable[[str], str]
    mime_type: Callable[[str, str | None], str | None]
    created: Callable[[File], None]
    invalidate: Callable[[list[int], int], None]


def _write_metadata(
    path: str, metadata: Mapping[str, object], root: str | None,
) -> None:
    """Persist session metadata through the shared multipart storage helper."""
    multipart_storage.write_metadata(path, dict(metadata), root=root)


def _multipart_metadata(reservation: UploadReservation) -> MultipartMetadata:
    """Project the JSON details this service wrote for the reservation."""
    return cast("MultipartMetadata", reservation.details)


def validate_upload_id(upload_id: str) -> str:
    """Return the trimmed upload id, rejecting values outside the safe charset."""
    candidate = (upload_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", candidate):
        message = "Invalid upload_id"
        raise BusinessRuleError(message)
    return candidate


def _require_all_chunks(uploaded_chunks: set[int], total_chunks: int) -> None:
    """Reject assembly while any chunk index is still missing."""
    missing = [index for index in range(total_chunks) if index not in uploaded_chunks]
    if missing:
        preview = ",".join(str(index) for index in missing[:10])
        message = f"Missing chunks: {preview}"
        raise BusinessRuleError(message)


def _verify_content_hash(actual_hash: str, expected_hash: str | None) -> None:
    """Reject a merged object whose digest differs from the declared hash."""
    if expected_hash and actual_hash != expected_hash:
        message = "Uploaded content does not match its hash"
        raise BusinessRuleError(message)


class MultipartUploadService:
    """Multipart sessions over quota reservations and shared publication ports."""

    def __init__(
        self,
        settings: MultipartSettings,
        files: MultipartFileOperations,
        *,
        storage_factory: Callable[[], MinIOStorageClient],
        write_metadata: Callable[
            [str, Mapping[str, object], str | None], None,
        ] = _write_metadata,
    ) -> None:
        """Store the settings, publication callbacks and metadata writer."""
        self.settings = settings
        self.files = files
        self.storage_factory = storage_factory
        self.write_metadata = write_metadata

    def directory(self, workspace_id: int, upload_id: str) -> str:
        """Return the chunk directory of the given session."""
        return multipart_storage.upload_directory(
            self.settings.multipart_root, workspace_id, upload_id,
        )

    def _pending_upload(self, data: MultipartInitData) -> PendingMultipart:
        """Validate the requested sizes and derive the chunk count."""
        filename = (data.get("filename") or "").strip()
        if not filename:
            message = "filename is required"
            raise BusinessRuleError(message)

        total_size = int(data.get("total_size") or 0)
        if total_size <= 0:
            message = "total_size must be positive"
            raise BusinessRuleError(message)
        if 0 < self.settings.max_upload_size < total_size:
            message = "File too large"
            raise PayloadTooLargeError(message)

        chunk_size = int(data.get("chunk_size") or self.settings.default_chunk_size)
        if chunk_size <= 0:
            message = "chunk_size must be positive"
            raise BusinessRuleError(message)
        if chunk_size > self.settings.max_chunk_size:
            message = "chunk_size exceeds server limit"
            raise BusinessRuleError(message)

        total_chunks = math.ceil(total_size / chunk_size)
        if total_chunks <= 0 or total_chunks > self.settings.max_total_chunks:
            message = "total_chunks exceeds server limit"
            raise BusinessRuleError(message)
        return PendingMultipart(
            filename=filename,
            total_size=total_size,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
        )

    def _try_instant_upload(
        self,
        session: Session,
        pending: PendingMultipart,
        publication: MultipartPublication,
    ) -> dict[str, object] | None:
        """Clone a reusable object when the declared hash is already stored."""
        content_hash = publication.content_hash
        if not content_hash:
            return None
        source_file = self.files.reusable_source(
            session, content_hash, pending.total_size, publication.workspace_id,
        )
        if not source_file:
            return None
        new_file = self.files.clone_source(
            session,
            CloneSourceParams(
                source_file=source_file,
                filename=pending.filename,
                workspace_id=publication.workspace_id,
                uploader_id=publication.uploader_id,
                parent_id=publication.parent_id,
                mime_type=publication.mime_type,
                content_hash=content_hash,
            ),
        )
        return {
            "upload_id": None,
            "chunk_size": pending.chunk_size,
            "total_chunks": pending.total_chunks,
            "uploaded_chunks": [],
            "instant_upload": True,
            "file": new_file.to_dict(),
        }

    def init_multipart_upload(
        self,
        session: Session,
        workspace_id: int,
        uploader_id: int,
        data: MultipartInitData,
    ) -> dict[str, object]:
        """Open (or reuse) the session that will receive the chunks."""
        pending = self._pending_upload(data)
        publication = MultipartPublication(
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            parent_id=self.files.resolve_parent(
                session, workspace_id, data.get("parent_id"),
            ),
            mime_type=data.get("mime_type"),
            content_hash=self.files.normalize_hash(data.get("content_hash")),
        )

        instant = self._try_instant_upload(session, pending, publication)
        if instant is not None:
            return instant

        upload_id = validate_upload_id(data.get("upload_id") or uuid.uuid4().hex)
        upload_dir = self.directory(workspace_id, upload_id)
        chunks_dir = multipart_storage.chunks_directory(upload_dir)
        meta_path = multipart_storage.metadata_path(upload_dir)

        existing = upload_quota.find_reservation(session, upload_id)
        metadata = MultipartMetadata(
            upload_id=upload_id,
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            filename=pending.filename,
            total_size=pending.total_size,
            chunk_size=pending.chunk_size,
            total_chunks=pending.total_chunks,
            parent_id=publication.parent_id,
            mime_type=publication.mime_type,
            content_hash=publication.content_hash,
            object_name=(
                _multipart_metadata(existing)["object_name"]
                if existing is not None
                else self.files.object_name(pending.filename)
            ),
            stage_id=(
                _multipart_metadata(existing)["stage_id"]
                if existing is not None
                else uuid.uuid4().hex
            ),
        )
        if existing is not None:
            reservation = upload_quota.owned(
                session, workspace_id, uploader_id, upload_id,
            )
            if reservation.kind != "multipart" or reservation.details != metadata:
                message = "upload_id already exists with different file metadata"
                raise ConflictError(message)
            session.commit()
        else:
            upload_authorization.reserve(
                session,
                ReserveParams(
                    workspace_id=workspace_id,
                    user_id=uploader_id,
                    size=pending.total_size,
                    kind="multipart",
                    reservation_id=upload_id,
                    details=metadata,
                ),
            )
            try:
                multipart_storage.prepare_chunks(
                    chunks_dir, root=self.settings.multipart_root,
                )
                self.write_metadata(
                    meta_path, metadata, self.settings.multipart_root,
                )
            except Exception as exc:
                compensate_failed_uploads(
                    session,
                    workspace_id,
                    uploader_id,
                    [(upload_id, metadata["object_name"])],
                    None,
                )
                remove_multipart_upload_safely(
                    self.settings.multipart_root, workspace_id, upload_id,
                )
                message = "Failed to initialize upload"
                raise ServiceOperationError(message) from exc

        return {
            "upload_id": upload_id,
            "chunk_size": pending.chunk_size,
            "total_chunks": pending.total_chunks,
            "uploaded_chunks": multipart_storage.list_chunks(
                chunks_dir, root=self.settings.multipart_root,
            ),
            "instant_upload": False,
        }

    def get_multipart_upload_status(
        self, session: Session, workspace_id: int, uploader_id: int, upload_id: str,
    ) -> dict[str, object]:
        """Return the recorded chunk progress of an open session."""
        meta = _multipart_metadata(
            upload_quota.owned(session, workspace_id, uploader_id, upload_id),
        )
        session.commit()
        upload_dir = self.directory(workspace_id, validate_upload_id(upload_id))
        chunks_dir = multipart_storage.chunks_directory(upload_dir)
        return {
            "upload_id": meta["upload_id"],
            "chunk_size": int(meta["chunk_size"]),
            "total_chunks": int(meta["total_chunks"]),
            "uploaded_chunks": multipart_storage.list_chunks(
                chunks_dir, root=self.settings.multipart_root,
            ),
        }

    def save_multipart_chunk(
        self, session: Session, params: SaveMultipartChunkParams,
    ) -> dict[str, object]:
        """Store one chunk inside the still-held reservation locks."""
        safe_upload_id = validate_upload_id(params.upload_id)
        meta = _multipart_metadata(
            upload_quota.owned(
                session, params.workspace_id, params.uploader_id, safe_upload_id,
            ),
        )

        try:
            index = int(params.chunk_index)
        except (TypeError, ValueError) as exc:
            message = "Invalid chunk_index"
            raise BusinessRuleError(message) from exc

        total_chunks = int(meta["total_chunks"])
        chunk_size = int(meta["chunk_size"])
        total_size = int(meta["total_size"])

        if index < 0 or index >= total_chunks:
            message = "chunk_index out of range"
            raise BusinessRuleError(message)

        upload_dir = self.directory(params.workspace_id, safe_upload_id)
        chunks_dir = multipart_storage.chunks_directory(upload_dir)
        expected_size = chunk_size
        if index == total_chunks - 1:
            expected_size = total_size - (index * chunk_size)
            if expected_size <= 0:
                expected_size = chunk_size

        multipart_storage.save_chunk(
            chunks_dir,
            multipart_storage.SaveChunkParams(
                index=index,
                upload=params.chunk,
                expected_size=expected_size,
                max_size=self.settings.max_chunk_size,
                root=self.settings.multipart_root,
            ),
        )
        session.commit()

        return {
            "upload_id": safe_upload_id,
            "chunk_index": index,
            "uploaded_chunks": multipart_storage.list_chunks(
                chunks_dir, root=self.settings.multipart_root,
            ),
        }

    def _assemble(
        self, staging: StagedUpload, chunks_dir: str, meta: MultipartMetadata,
    ) -> None:
        """Merge the stored chunks into the staging payload."""
        try:
            multipart_storage.assemble(
                chunks_dir,
                staging.path,
                multipart_storage.AssembleParams(
                    total_chunks=int(meta["total_chunks"]),
                    chunk_size=int(meta["chunk_size"]),
                    total_size=int(meta["total_size"]),
                    root=self.settings.multipart_root,
                ),
            )
        except DomainError:
            close_staging_safely(staging)
            raise
        except Exception as exc:
            close_staging_safely(staging)
            logger.error(
                "Failed to merge chunks (error_type={})", type(exc).__name__,
            )
            message = "Failed to merge chunks"
            raise ServiceOperationError(message) from exc
        except BaseException:
            close_staging_safely(staging)
            raise

    def _publish_merged(
        self,
        session: Session,
        staging: StagedUpload,
        publication: MultipartPublication,
        meta: MultipartMetadata,
    ) -> File:
        """Publish the merged object, then insert and commit its file row."""
        unique_filename = meta["object_name"]
        total_size = int(meta["total_size"])
        storage: MinIOStorageClient | None = None
        try:
            # Publish the merged file to MinIO.
            actual_hash = self.files.hash_file(staging.path)
            _verify_content_hash(actual_hash, publication.content_hash)
            storage = self.storage_factory()
            storage.upload_file(staging.path, unique_filename)

            resolved_mime = self.files.mime_type(
                staging.path, publication.mime_type,
            )
            new_file = File(
                name=meta["filename"],
                file_path=unique_filename,
                file_size=total_size,
                mime_type=resolved_mime,
                content_hash=actual_hash,
                workspace_id=publication.workspace_id,
                uploader_id=publication.uploader_id,
                parent_id=publication.parent_id,
            )
            upload_authorization.consume(
                session,
                publication.workspace_id,
                publication.uploader_id,
                meta["upload_id"],
                total_size,
            )
            new_file.parent_id = self.files.resolve_parent(
                session, publication.workspace_id, publication.parent_id,
            )
            repository.insert_file(session, new_file)
            stage_file_tasks(session, [new_file.id])
            session.commit()
        except Exception as exc:
            compensate_failed_uploads(
                session,
                publication.workspace_id,
                publication.uploader_id,
                [(meta["upload_id"], unique_filename)],
                storage,
            )
            logger.error(
                "Failed to persist merged file (error_type={})", type(exc).__name__,
            )
            if isinstance(exc, DomainError):
                raise
            message = "Failed to save file metadata"
            raise ServiceOperationError(message) from exc
        finally:
            # Remove the merged staging file and the chunk directory.
            close_staging_safely(staging)
            remove_multipart_upload_safely(
                self.settings.multipart_root,
                publication.workspace_id,
                meta["upload_id"],
            )

        self.files.created(new_file)
        self.files.invalidate([new_file.id], publication.workspace_id)
        return new_file

    def complete_multipart_upload(
        self, session: Session, workspace_id: int, uploader_id: int, upload_id: str,
    ) -> File:
        """Assemble the chunks, then publish the merged object and its row."""
        safe_upload_id = validate_upload_id(upload_id)
        meta = _multipart_metadata(
            upload_quota.owned(session, workspace_id, uploader_id, safe_upload_id),
        )
        if not meta.get("object_name") or not meta.get("stage_id"):
            message = "Upload session must be reinitialized after upgrade"
            raise ConflictError(message)

        total_size = int(meta["total_size"])
        if total_size <= 0 or total_size > self.settings.max_upload_size:
            message = "File too large"
            raise PayloadTooLargeError(message)
        publication = MultipartPublication(
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            parent_id=self.files.resolve_parent(
                session, workspace_id, meta.get("parent_id"),
            ),
            mime_type=meta.get("mime_type"),
            content_hash=self.files.normalize_hash(meta.get("content_hash")),
        )

        upload_dir = self.directory(workspace_id, safe_upload_id)
        chunks_dir = multipart_storage.chunks_directory(upload_dir)
        uploaded_chunks = set(
            multipart_storage.list_chunks(
                chunks_dir, root=self.settings.multipart_root,
            ),
        )
        _require_all_chunks(uploaded_chunks, int(meta["total_chunks"]))

        Path(self.settings.upload_root).mkdir(parents=True, exist_ok=True)
        staging = StagedUpload(
            self.settings.upload_root, meta["filename"], stage_id=meta["stage_id"],
        )
        self._assemble(staging, chunks_dir, meta)
        return self._publish_merged(session, staging, publication, meta)

    def abort_multipart_upload(
        self, session: Session, workspace_id: int, uploader_id: int, upload_id: str,
    ) -> None:
        """Cancel the session reservation and delete its chunk directory."""
        safe_upload_id = validate_upload_id(upload_id)
        upload_quota.cancel(
            session,
            CancelParams(
                workspace_id=workspace_id,
                user_id=uploader_id,
                reservation_id=safe_upload_id,
            ),
        )
        remove_multipart_upload_safely(
            self.settings.multipart_root, workspace_id, safe_upload_id,
        )
