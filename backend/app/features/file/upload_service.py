"""Single and batch upload use cases with explicit transaction ownership."""

from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    ConflictError,
    DomainError,
    PayloadTooLargeError,
    PermissionDeniedError,
    ServiceOperationError,
)
from app.features.file import repository, upload_authorization
from app.features.file.deletion import clear_search_cache
from app.features.file.publication import (
    normalize_hash,
    record_batch_created,
    record_created,
    reserve_and_store,
    resolve_parent,
    stage_record,
    stage_source,
    stage_stored_files,
)
from app.features.file.repository import FileRecordSpec
from app.features.file.upload_config import UploadSettings, upload_settings
from app.features.file.upload_quota import ReserveParams
from app.features.file.upload_recovery import compensate_failed_uploads
from app.features.file.upload_types import (
    PreflightData,
    StoredUpload,
    UploadScope,
    UploadSource,
    UploadStorage,
)
from app.features.workspace.permissions import assert_can_write_locked
from app.infra.file_object_deletion import lock_object_path
from app.infra.storage import get_storage_client
from app.models.file import File, VectorValue


@dataclass(frozen=True, slots=True, kw_only=True)
class CloneSourceParams:
    """Inputs for cloning an existing object into a new file row."""

    source_file: File
    filename: str
    workspace_id: int
    uploader_id: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str


def _validate_live_source(
    live_source: File | None,
    workspace_id: int,
    object_name: str,
    file_exists: Callable[[str], bool],
) -> File:
    """Return the refreshed clone source or raise when it is no longer usable."""
    if live_source is None:
        message = "Reusable file no longer available; retry upload"
        raise ConflictError(message)
    if live_source.workspace_id != workspace_id:
        message = "Reusable file belongs to another workspace"
        raise PermissionDeniedError(message)
    if live_source.file_path != object_name or not file_exists(object_name):
        message = "Reusable file no longer available; retry upload"
        raise ConflictError(message)
    return live_source


class FileUploadService:
    """Single and batch object publication over durable reservations."""

    def __init__(
        self,
        settings: UploadSettings,
        *,
        storage_factory: Callable[[], UploadStorage] = get_storage_client,
    ) -> None:
        """Bind upload settings and the minimal object-store boundary."""
        self.settings = settings
        self.storage_factory = storage_factory

    def create_uploaded_file(
        self,
        session: Session,
        workspace_id: int,
        uploader_id: int,
        upload: UploadSource,
        parent_id: int | None = None,
    ) -> File:
        """Stage, reserve, store and commit one file publication."""
        if not upload.filename:
            message = "No selected file"
            raise BusinessRuleError(message)
        scope = UploadScope(
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            parent_id=resolve_parent(session, workspace_id, parent_id),
        )
        storage = self.storage_factory()
        stored: StoredUpload | None = None
        try:
            with stage_source(
                upload, self.settings, max_bytes=self.settings.max_upload_size,
            ) as staged:
                stored = reserve_and_store(session, staged, scope, storage)
                files = stage_stored_files(session, scope, [stored])
                session.commit()
        except Exception as exc:
            if stored is not None:
                compensate_failed_uploads(
                    session,
                    workspace_id,
                    uploader_id,
                    [(stored.reservation_id, stored.spec.file_path)],
                    storage,
                )
            if isinstance(exc, DomainError):
                raise
            logger.error(
                "Failed to save uploaded file metadata (error_type={})",
                type(exc).__name__,
            )
            message = "Failed to save file metadata"
            raise ServiceOperationError(message) from exc

        file = files[0]
        record_created(file)
        clear_search_cache(workspace_id)
        return file

    def create_uploaded_files(
        self,
        session: Session,
        workspace_id: int,
        uploader_id: int,
        uploads: list[UploadSource],
        parent_id: int | None = None,
    ) -> list[File]:
        """Store every valid upload and publish all rows in one transaction."""
        valid_uploads = [upload for upload in uploads if upload and upload.filename]
        if not valid_uploads:
            message = "No selected files"
            raise BusinessRuleError(message)
        scope = UploadScope(
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            parent_id=resolve_parent(session, workspace_id, parent_id),
        )
        storage = self.storage_factory()
        stored: list[StoredUpload] = []
        for upload in valid_uploads:
            try:
                with stage_source(
                    upload, self.settings, max_bytes=self.settings.max_upload_size,
                ) as staged:
                    stored.append(reserve_and_store(session, staged, scope, storage))
            except Exception as exc:
                compensate_failed_uploads(
                    session,
                    workspace_id,
                    uploader_id,
                    [
                        (item.reservation_id, item.spec.file_path)
                        for item in stored
                    ],
                    storage,
                )
                if isinstance(exc, DomainError):
                    raise
                message = "Failed to upload file"
                raise ServiceOperationError(message) from exc

        try:
            files = stage_stored_files(session, scope, stored)
            session.commit()
        except Exception as exc:
            compensate_failed_uploads(
                session,
                workspace_id,
                uploader_id,
                [
                    (item.reservation_id, item.spec.file_path)
                    for item in stored
                ],
                storage,
            )
            if isinstance(exc, DomainError):
                raise
            message = "Failed to save uploaded file metadata"
            raise ServiceOperationError(message) from exc

        record_batch_created(workspace_id, files)
        clear_search_cache(workspace_id)
        return files

    def reusable_source(
        self, session: Session, content_hash: str, file_size: int, workspace_id: int,
    ) -> File | None:
        """Return a stored object that can satisfy the upload without a PUT."""
        source_file = repository.find_reusable_file(
            session, content_hash, file_size, workspace_id,
        )
        if source_file is None:
            return None
        if not self.storage_factory().file_exists(source_file.file_path):
            return None
        return source_file

    def clone_source(self, session: Session, params: CloneSourceParams) -> File:
        """Commit an instant-upload row while preserving its shared source object."""
        source_file = params.source_file
        if source_file.workspace_id != params.workspace_id:
            message = "Reusable file belongs to another workspace"
            raise PermissionDeniedError(message)
        source_id = source_file.id
        object_path = source_file.file_path
        file_size = int(source_file.file_size or 0)

        reservation = upload_authorization.reserve(
            session,
            ReserveParams(
                workspace_id=params.workspace_id,
                user_id=params.uploader_id,
                size=file_size,
                kind="instant",
            ),
        )
        # Cache the identity now: a failed flush/commit expires this row, and
        # reading reservation.id inside the handler would raise before the
        # compensation could reset the transaction.
        reservation_id = reservation.id
        try:
            assert_can_write_locked(
                session, params.workspace_id, params.uploader_id,
            )
            lock_object_path(session, object_path)
            live_source = _validate_live_source(
                repository.find_live_file(session, source_id),
                params.workspace_id,
                object_path,
                lambda name: self.storage_factory().file_exists(name),
            )
            resolved_mime = params.mime_type or live_source.mime_type
            status = live_source.status or "pending"
            description = live_source.description if status == "success" else None
            vector_info: VectorValue | None = (
                live_source.vector_info if status == "success" else None
            )
            upload_authorization.consume(
                session,
                params.workspace_id,
                params.uploader_id,
                reservation_id,
                int(live_source.file_size or 0),
            )
            new_file = stage_record(
                session,
                FileRecordSpec(
                    name=params.filename,
                    file_path=live_source.file_path,
                    file_size=int(live_source.file_size or 0),
                    mime_type=resolved_mime,
                    workspace_id=params.workspace_id,
                    uploader_id=params.uploader_id,
                    parent_id=params.parent_id,
                    content_hash=params.content_hash,
                    status=status,
                    description=description,
                    vector_info=vector_info,
                ),
            )
            session.commit()
        except Exception as exc:
            compensate_failed_uploads(
                session,
                params.workspace_id,
                params.uploader_id,
                [(reservation_id, object_path)],
                None,
            )
            if isinstance(exc, DomainError):
                raise
            logger.error(
                "Failed to create instant upload record (error_type={})",
                type(exc).__name__,
            )
            message = "Failed to save file metadata"
            raise ServiceOperationError(message) from exc

        record_created(new_file)
        clear_search_cache(params.workspace_id)
        return new_file

    def preflight_file_upload(
        self,
        session: Session,
        workspace_id: int,
        uploader_id: int,
        data: PreflightData,
    ) -> dict[str, object]:
        """Clone a stored object when the hash matches; skip the chunk transfer."""
        filename = (data.get("filename") or "").strip()
        if not filename:
            message = "filename is required"
            raise BusinessRuleError(message)

        total_size = int(data.get("total_size") or 0)
        if total_size <= 0:
            message = "total_size must be positive"
            raise BusinessRuleError(message)
        if total_size > self.settings.max_upload_size:
            message = "File too large"
            raise PayloadTooLargeError(message)
        content_hash = normalize_hash(data.get("content_hash"))
        if not content_hash:
            message = "content_hash is required"
            raise BusinessRuleError(message)

        source_file = self.reusable_source(
            session, content_hash, total_size, workspace_id,
        )
        if source_file is None:
            return {"instant_upload": False, "exists": False}

        resolved_parent = resolve_parent(
            session, workspace_id, data.get("parent_id"),
        )
        new_file = self.clone_source(
            session,
            CloneSourceParams(
                source_file=source_file,
                filename=filename,
                workspace_id=workspace_id,
                uploader_id=uploader_id,
                parent_id=resolved_parent,
                mime_type=data.get("mime_type"),
                content_hash=content_hash,
            ),
        )
        return {"instant_upload": True, "exists": True, "file": new_file.to_dict()}


def get_upload_service() -> FileUploadService:
    """Build the upload use case through its sole settings assembly point."""
    return FileUploadService(upload_settings())
