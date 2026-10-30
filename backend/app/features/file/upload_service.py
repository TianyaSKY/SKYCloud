"""Single and batch upload orchestration with explicit publication/byte ports.

The caller owns the SQL Session; durable reservations cover each object PUT.
Batch failures compensate every accepted object before reporting the result.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict

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
from app.features.file.repository import FileRecordSpec
from app.features.file.upload_quota import ReserveParams
from app.features.file.upload_recovery import compensate_failed_uploads
from app.features.folder.change_log import FileChangeEventSpec
from app.features.workspace.permissions import assert_can_write_locked
from app.infra.file_object_deletion import lock_object_path
from app.infra.file_task_outbox import stage_file_tasks
from app.infra.storage import MinIOStorageClient
from app.infra.upload_adapter import save_bounded
from app.infra.upload_limits import FILE_LIMIT
from app.infra.upload_staging import StagedUpload, close_staging_safely
from app.models.file import File, VectorValue


class UploadSource(Protocol):
    """Upload adapter surface used by the bounded byte writer."""

    filename: str

    @property
    def mimetype(self) -> str | None:
        """Return the upload's declared content type, when present."""
        ...

    def save(self, destination: str, max_bytes: int = FILE_LIMIT) -> None:
        """Write the payload to ``destination`` without exceeding ``max_bytes``."""
        ...


class UploadData(TypedDict):
    """Publication inputs shared by the HTTP, MCP and avatar entry points."""

    workspace_id: int
    uploader_id: int
    parent_id: NotRequired[int | None]
    max_bytes: NotRequired[int]


class BatchUploadData(TypedDict, total=False):
    """Batch publication inputs; the owner ids are validated by the service."""

    workspace_id: int
    uploader_id: int
    parent_id: int | None


class PreflightData(TypedDict, total=False):
    """Preflight inputs decoded from the request payload."""

    filename: str
    total_size: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _BatchOwner:
    """Workspace/user pair that owns one batch upload."""

    workspace_id: int
    uploader_id: int
    parent_id: int | None


@dataclass(slots=True)
class _StagedBatch:
    """Reservation keys and rows accumulated while a batch is stored."""

    reservations: list[str] = field(default_factory=list[str])
    object_keys: list[str] = field(default_factory=list[str])
    new_files: list[File] = field(default_factory=list[File])


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


@dataclass(frozen=True, slots=True)
class UploadSettings:
    """Filesystem staging root and default byte ceiling for one upload."""

    upload_root: str
    max_upload_size: int


@dataclass(frozen=True, slots=True)
class FilePublicationOperations:
    """Publication callbacks shared by every upload entry point."""

    object_name: Callable[[str], str]
    mime_type: Callable[[str, str | None], str | None]
    hash_file: Callable[[str], str]
    persist_record: Callable[[Session, FileRecordSpec], File]
    created: Callable[[File], None]
    journal_batch: Callable[[int, Sequence[FileChangeEventSpec]], int]
    invalidate: Callable[[list[int], int | None], None]
    normalize_hash: Callable[[str | None], str | None]
    resolve_parent: Callable[[Session, int, int | None], int | None]
    clear_search: Callable[[int | None], None]


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
        files: FilePublicationOperations,
        *,
        storage_factory: Callable[[], MinIOStorageClient],
        staging_factory: Callable[[str, str], StagedUpload] = StagedUpload,
        write_upload: Callable[[UploadSource, str, int], None] = save_bounded,
    ) -> None:
        """Store the settings, publication callbacks and byte adapters."""
        self.settings = settings
        self.files = files
        self.storage_factory = storage_factory
        self.staging_factory = staging_factory
        self.write_upload = write_upload

    def create_file(
        self,
        session: Session,
        file_obj: UploadSource,
        data: UploadData,
        *,
        before_commit: Callable[[File], None] | None = None,
        authorize_upload: Callable[[], None] | None = None,
    ) -> File:
        """Store one bounded upload, then publish its file row."""
        storage = self.storage_factory()
        Path(self.settings.upload_root).mkdir(parents=True, exist_ok=True)

        original_filename = file_obj.filename or "unknown"
        unique_filename = self.files.object_name(original_filename)
        staging = self.staging_factory(self.settings.upload_root, original_filename)
        local_tmp_path = staging.path

        reservation_id = None
        try:
            self.write_upload(
                file_obj,
                local_tmp_path,
                data.get("max_bytes", self.settings.max_upload_size),
            )
            file_size = Path(local_tmp_path).stat().st_size
            mime_type = self.files.mime_type(
                local_tmp_path, getattr(file_obj, "mimetype", None),
            )
            content_hash = self.files.hash_file(local_tmp_path)
            if authorize_upload is not None:
                authorize_upload()
            reservation = upload_authorization.reserve(
                session,
                ReserveParams(
                    workspace_id=data["workspace_id"],
                    user_id=data["uploader_id"],
                    size=file_size,
                    reservation_id=staging.directory.name,
                    details={
                        "object_name": unique_filename,
                        "stage_id": staging.directory.name,
                    },
                ),
            )
            reservation_id = reservation.id
            # Upload to the MinIO object store.
            storage.upload_file(local_tmp_path, unique_filename)

            if authorize_upload is not None:
                authorize_upload()
            upload_authorization.consume(
                session,
                data["workspace_id"],
                data["uploader_id"],
                reservation_id,
                file_size,
            )
            new_file = self.files.persist_record(
                session,
                FileRecordSpec(
                    name=original_filename,
                    file_path=unique_filename,
                    file_size=file_size,
                    mime_type=mime_type,
                    workspace_id=data["workspace_id"],
                    uploader_id=data["uploader_id"],
                    parent_id=data.get("parent_id"),
                    content_hash=content_hash,
                    before_commit=before_commit,
                ),
            )
        except Exception as exc:
            compensate_failed_uploads(
                session,
                data["workspace_id"],
                data["uploader_id"],
                [(reservation_id, unique_filename)],
                storage,
            )
            logger.error(
                "Failed to save uploaded file metadata (error_type={})",
                type(exc).__name__,
            )
            if isinstance(exc, DomainError):
                raise
            message = "Failed to save file metadata"
            raise ServiceOperationError(message) from exc
        finally:
            # Remove the private staging directory.
            close_staging_safely(staging)

        self.files.created(new_file)
        self.files.invalidate([new_file.id], new_file.workspace_id)
        return new_file

    def _journal_batch_created(
        self, workspace_id: int, new_files: list[File],
    ) -> None:
        """Record batch creation events after the caller's commit."""
        self.files.journal_batch(
            workspace_id,
            [
                {
                    "entity_type": "file",
                    "entity_id": file_obj.id,
                    "action": "create",
                    "old_parent_id": None,
                    "new_parent_id": file_obj.parent_id,
                    "old_name": None,
                    "new_name": file_obj.name,
                }
                for file_obj in new_files
            ],
        )

    def _stage_batch_files(
        self,
        session: Session,
        storage: MinIOStorageClient,
        file_objs: list[UploadSource],
        owner: _BatchOwner,
        staged: _StagedBatch,
    ) -> None:
        """Write every batch upload, accumulating the accepted reservations."""
        for file_obj in file_objs:
            staging = None
            try:
                original_filename = file_obj.filename
                unique_filename = self.files.object_name(original_filename)
                staging = self.staging_factory(
                    self.settings.upload_root, original_filename,
                )
                local_tmp_path = staging.path
                self.write_upload(
                    file_obj, local_tmp_path, self.settings.max_upload_size,
                )
                file_size = Path(local_tmp_path).stat().st_size
                mime_type = self.files.mime_type(
                    local_tmp_path, getattr(file_obj, "mimetype", None),
                )
                content_hash = self.files.hash_file(local_tmp_path)

                reservation = upload_authorization.reserve(
                    session,
                    ReserveParams(
                        workspace_id=owner.workspace_id,
                        user_id=owner.uploader_id,
                        size=file_size,
                        reservation_id=staging.directory.name,
                        details={
                            "object_name": unique_filename,
                            "stage_id": staging.directory.name,
                        },
                    ),
                )
                staged.reservations.append(reservation.id)
                staged.object_keys.append(unique_filename)
                storage.upload_file(local_tmp_path, unique_filename)
                staged.new_files.append(
                    File(
                        name=original_filename,
                        file_path=unique_filename,
                        file_size=file_size,
                        mime_type=mime_type,
                        content_hash=content_hash,
                        workspace_id=owner.workspace_id,
                        uploader_id=owner.uploader_id,
                        parent_id=owner.parent_id,
                    ),
                )
            except Exception as exc:
                compensate_failed_uploads(
                    session,
                    owner.workspace_id,
                    owner.uploader_id,
                    zip(staged.reservations, staged.object_keys, strict=False),
                    storage,
                )
                if isinstance(exc, DomainError):
                    raise
                message = "Failed to upload file"
                raise ServiceOperationError(message) from exc
            finally:
                if staging is not None:
                    close_staging_safely(staging)

    def _publish_staged_batch(
        self,
        session: Session,
        storage: MinIOStorageClient,
        owner: _BatchOwner,
        staged: _StagedBatch,
    ) -> None:
        """Consume the reservations and commit every staged row at once."""
        try:
            for reservation_id, file_obj in zip(
                staged.reservations, staged.new_files, strict=False,
            ):
                upload_authorization.consume(
                    session,
                    owner.workspace_id,
                    owner.uploader_id,
                    reservation_id,
                    int(file_obj.file_size or 0),
                )
            if owner.parent_id is not None:
                parent_id = self.files.resolve_parent(
                    session, owner.workspace_id, owner.parent_id,
                )
                for file_obj in staged.new_files:
                    file_obj.parent_id = parent_id
            repository.insert_files(session, staged.new_files)
            stage_file_tasks(session, [file.id for file in staged.new_files])
            session.commit()
        except Exception as exc:
            compensate_failed_uploads(
                session,
                owner.workspace_id,
                owner.uploader_id,
                zip(staged.reservations, staged.object_keys, strict=False),
                storage,
            )
            if isinstance(exc, DomainError):
                raise
            message = "Failed to save uploaded file metadata"
            raise ServiceOperationError(message) from exc

    def batch_create_files(
        self,
        session: Session,
        file_objs: list[UploadSource],
        data: BatchUploadData,
    ) -> list[File]:
        """Store every valid upload and publish the rows in one transaction."""
        valid_files = [
            file_obj for file_obj in file_objs if file_obj and file_obj.filename
        ]
        if not valid_files:
            return []

        storage = self.storage_factory()
        Path(self.settings.upload_root).mkdir(parents=True, exist_ok=True)

        workspace_id = data.get("workspace_id")
        uploader_id = data.get("uploader_id")
        if workspace_id is None or uploader_id is None:
            message = "workspace_id and uploader_id are required"
            raise BusinessRuleError(message)

        owner = _BatchOwner(
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            parent_id=data.get("parent_id"),
        )
        staged = _StagedBatch()
        self._stage_batch_files(session, storage, valid_files, owner, staged)
        self._publish_staged_batch(session, storage, owner, staged)

        if uploader_id:
            self._journal_batch_created(workspace_id, staged.new_files)

        self.files.invalidate(
            [file_obj.id for file_obj in staged.new_files], workspace_id,
        )
        return staged.new_files

    def reusable_source(
        self, session: Session, content_hash: str, file_size: int, workspace_id: int,
    ) -> File | None:
        """Return a stored object that can satisfy the upload without a PUT."""
        source_file = repository.find_reusable_file(
            session, content_hash, file_size, workspace_id,
        )
        if not source_file:
            return None
        storage = self.storage_factory()
        if not storage.file_exists(source_file.file_path):
            return None
        return source_file

    def clone_source(self, session: Session, params: CloneSourceParams) -> File:
        """Instant upload: reuse the stored object path for a new file row."""
        source_file = params.source_file
        if source_file.workspace_id != params.workspace_id:
            message = "Reusable file belongs to another workspace"
            raise PermissionDeniedError(message)
        source_id = source_file.id
        object_name = source_file.file_path
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
        reservation_id = reservation.id
        try:
            # Identity locks precede the shared-object lock, matching cancellation.
            assert_can_write_locked(session, params.workspace_id, params.uploader_id)
            lock_object_path(session, object_name)
            live_source = _validate_live_source(
                repository.find_live_file(session, source_id),
                params.workspace_id,
                object_name,
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
            new_file = self.files.persist_record(
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
        except Exception as exc:
            compensate_failed_uploads(
                session,
                params.workspace_id,
                params.uploader_id,
                [(reservation_id, object_name)],
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

        self.files.created(new_file)
        if status != "success":
            self.files.invalidate([new_file.id], params.workspace_id)
        else:
            self.files.clear_search(params.workspace_id)
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
        content_hash = self.files.normalize_hash(data.get("content_hash"))
        if not content_hash:
            message = "content_hash is required"
            raise BusinessRuleError(message)

        source_file = self.reusable_source(
            session, content_hash, total_size, workspace_id,
        )
        if not source_file:
            return {"instant_upload": False, "exists": False}

        parent_id = self.files.resolve_parent(
            session, workspace_id, data.get("parent_id"),
        )
        new_file = self.clone_source(
            session,
            CloneSourceParams(
                source_file=source_file,
                filename=filename,
                workspace_id=workspace_id,
                uploader_id=uploader_id,
                parent_id=parent_id,
                mime_type=data.get("mime_type"),
                content_hash=content_hash,
            ),
        )
        return {"instant_upload": True, "exists": True, "file": new_file.to_dict()}
