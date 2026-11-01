"""Pure file-publication stages over caller-owned SQL and object storage."""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from werkzeug.utils import secure_filename

from app.exceptions import BusinessRuleError, PermissionDeniedError
from app.features.file import repository, upload_authorization
from app.features.file.repository import FileRecordSpec
from app.features.file.tasks import stage_file_tasks
from app.features.file.upload_quota import ReserveParams
from app.features.file.upload_recovery import compensate_failed_uploads
from app.features.file.upload_types import (
    StagedFile,
    StoredUpload,
    UploadScope,
    UploadSource,
    UploadStorage,
)
from app.features.folder import change_log as change_log_service
from app.features.folder.service import get_root_folder_id
from app.infra.upload_adapter import save_bounded
from app.infra.upload_staging import StagedUpload, close_staging_safely
from app.models.file import File

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from sqlalchemy.orm import Session

    from app.features.file.upload_config import UploadSettings
    from app.features.folder.change_log import FileChangeEventSpec
COPY_BUFFER_SIZE = 1024 * 1024
SHA256_HEX_LENGTH = 64


def object_name(original_filename: str) -> str:
    """Return a collision-free object key derived from the original name."""
    extension = Path(original_filename).suffix
    base_name = original_filename[: len(original_filename) - len(extension)]
    safe_base_name = secure_filename(base_name)
    if safe_base_name:
        return f"{uuid.uuid4().hex}_{safe_base_name}{extension}"
    return f"{uuid.uuid4().hex}{extension}"


def mime_type(path: str, provided: str | None = None) -> str | None:
    """Return the declared MIME type or guess it from the path extension."""
    if provided:
        return provided
    return mimetypes.guess_type(path)[0]


def normalize_hash(content_hash: str | None) -> str | None:
    """Return the lowercase SHA-256 digest, rejecting malformed values."""
    if not content_hash:
        return None
    normalized = content_hash.strip().lower()
    if len(normalized) != SHA256_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        message = "Invalid content_hash"
        raise BusinessRuleError(message)
    return normalized


def hash_file(path: str) -> str:
    """Return the SHA-256 digest of the staged payload."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        while True:
            chunk = file_obj.read(COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def resolve_parent(
    session: Session, workspace_id: int, parent_id: int | None,
) -> int | None:
    """Resolve the workspace root and reject parents from another workspace."""
    if parent_id is None:
        return get_root_folder_id(session, workspace_id)
    parent = repository.find_folder(session, parent_id)
    if not parent or parent.workspace_id != workspace_id:
        message = "Parent folder belongs to another workspace"
        raise PermissionDeniedError(message)
    return parent_id


@contextmanager
def stage_source(
    source: UploadSource,
    settings: UploadSettings,
    *,
    max_bytes: int,
) -> Generator[StagedFile, None, None]:
    """Write bounded upload bytes and derive metadata without accessing SQL."""
    Path(settings.upload_root).mkdir(parents=True, exist_ok=True)
    name = source.filename or "unknown"
    staging = StagedUpload(settings.upload_root, name)
    try:
        save_bounded(source, staging.path, max_bytes)
        yield StagedFile(
            staging=staging,
            name=name,
            size=Path(staging.path).stat().st_size,
            mime_type=mime_type(staging.path, getattr(source, "mimetype", None)),
            content_hash=hash_file(staging.path),
        )
    finally:
        close_staging_safely(staging)


def reserve_and_store(
    session: Session,
    staged: StagedFile,
    scope: UploadScope,
    storage: UploadStorage,
) -> StoredUpload:
    """Durably reserve one object key before PUT; compensate uncertain failures."""
    key = object_name(staged.name)
    stage_id = staged.staging.directory.name
    reservation = upload_authorization.reserve(
        session,
        ReserveParams(
            workspace_id=scope.workspace_id,
            user_id=scope.uploader_id,
            size=staged.size,
            reservation_id=stage_id,
            details={"object_name": key, "stage_id": stage_id},
        ),
    )
    try:
        storage.upload_file(staged.staging.path, key)
    except Exception:
        compensate_failed_uploads(
            session,
            scope.workspace_id,
            scope.uploader_id,
            [(reservation.id, key)],
            storage,
        )
        raise
    return StoredUpload(
        reservation_id=reservation.id,
        spec=FileRecordSpec(
            name=staged.name,
            file_path=key,
            file_size=staged.size,
            mime_type=staged.mime_type,
            workspace_id=scope.workspace_id,
            uploader_id=scope.uploader_id,
            parent_id=scope.parent_id,
            content_hash=staged.content_hash,
        ),
    )


def stage_stored_files(
    session: Session,
    scope: UploadScope,
    stored: Sequence[StoredUpload],
) -> list[File]:
    """Consume reservations and stage all file/outbox rows in one transaction."""
    if not stored:
        return []
    for upload in stored:
        upload_authorization.consume(
            session,
            scope.workspace_id,
            scope.uploader_id,
            upload.reservation_id,
            upload.spec.file_size,
        )
    parent_id = resolve_parent(session, scope.workspace_id, scope.parent_id)
    files = [
        File(
            name=upload.spec.name,
            file_path=upload.spec.file_path,
            file_size=upload.spec.file_size,
            mime_type=upload.spec.mime_type,
            workspace_id=upload.spec.workspace_id,
            uploader_id=upload.spec.uploader_id,
            parent_id=parent_id,
            content_hash=upload.spec.content_hash,
            status=upload.spec.status,
            description=upload.spec.description,
            vector_info=upload.spec.vector_info,
        )
        for upload in stored
    ]
    repository.insert_files(session, files)
    pending_ids = [file.id for file in files if file.status != "success"]
    stage_file_tasks(session, pending_ids)
    return files


def stage_record(session: Session, spec: FileRecordSpec) -> File:
    """Stage one existing-object File and its index task without committing."""
    parent_id = spec.parent_id
    if parent_id is not None:
        parent_id = resolve_parent(session, spec.workspace_id, parent_id)
    file = File(
        name=spec.name,
        file_path=spec.file_path,
        file_size=spec.file_size,
        mime_type=spec.mime_type,
        workspace_id=spec.workspace_id,
        uploader_id=spec.uploader_id,
        parent_id=parent_id,
        content_hash=spec.content_hash,
        status=spec.status,
        description=spec.description,
        vector_info=spec.vector_info,
    )
    repository.insert_file(session, file)
    if spec.status != "success":
        stage_file_tasks(session, [file.id])
    return file


def record_created(file: File) -> None:
    """Record one successfully committed file-creation event."""
    if not file.workspace_id:
        return
    change_log_service.log_event(
        change_log_service.LogEventParams(
            workspace_id=file.workspace_id,
            entity_type="file",
            entity_id=file.id,
            action="create",
            old_parent_id=None,
            new_parent_id=file.parent_id,
            old_name=None,
            new_name=file.name,
        ),
    )


def record_batch_created(workspace_id: int, files: Sequence[File]) -> None:
    """Record batch creation events after the caller's commit."""
    events: list[FileChangeEventSpec] = [
        {
            "entity_type": "file",
            "entity_id": file.id,
            "action": "create",
            "old_parent_id": None,
            "new_parent_id": file.parent_id,
            "old_name": None,
            "new_name": file.name,
        }
        for file in files
    ]
    change_log_service.log_events_batch(workspace_id, events)
