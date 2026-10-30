"""File upload, download and multipart session entry points for the HTTP layer.

Upload and quota persistence live in upload_service/multipart_service, search
lives in search_service and deletion lives in deletion; nothing is re-exported.
"""

import hashlib
import mimetypes
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from loguru import logger
from sqlalchemy.orm import Session
from werkzeug.utils import secure_filename

from app.exceptions import (
    BusinessRuleError,
    DomainError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ServiceOperationError,
)
from app.features.auth import account_repository, user_service
from app.features.file import repository, upload_quota
from app.features.file.avatar_service import AvatarOperations, AvatarUploadService
from app.features.file.deletion import clear_search_cache, delete_file
from app.features.file.multipart_service import (
    MultipartFileOperations,
    MultipartInitData,
    MultipartSettings,
    MultipartUploadService,
    SaveMultipartChunkParams,
    validate_upload_id,
)
from app.features.file.repository import FileRecordSpec, ListDirectoryParams
from app.features.file.upload_service import (
    BatchUploadData,
    CloneSourceParams,
    FilePublicationOperations,
    FileUploadService,
    PreflightData,
    UploadData,
    UploadSettings,
    UploadSource,
)
from app.features.folder import change_log as change_log_service
from app.features.folder import service as folder_service
from app.features.share import service as share_service
from app.features.workspace.permissions import (
    assert_can_write_locked,
    assert_member,
)
from app.infra import multipart_storage
from app.infra.cache import (
    CacheableParams,
    cacheable,
    evict_cache,
    sql_cache_allowed,
    sql_cache_revision,
)
from app.infra.extensions import UPLOAD_FOLDER, SessionFactory
from app.infra.file_task_outbox import stage_file_tasks
from app.infra.multipart_cleanup import cleanup_legacy_multipart
from app.infra.owned_work import run_owned
from app.infra.storage import get_storage_client
from app.infra.upload_adapter import save_bounded
from app.infra.upload_limits import AVATAR_LIMIT, FILE_LIMIT, positive_limit
from app.infra.upload_staging import StagedUpload, cleanup_staging
from app.models.file import File

CACHE_EXPIRATION = 3600
ROOT_FILE_CACHE_PREFIX = "workspace:root:file:v2"

COPY_BUFFER_SIZE = 1024 * 1024

DEFAULT_CHUNK_SIZE = positive_limit("UPLOAD_CHUNK_SIZE", 2 * 1024 * 1024)
MAX_CHUNK_SIZE = positive_limit("UPLOAD_MAX_CHUNK_SIZE", 64 * 1024 * 1024)
MAX_UPLOAD_SIZE = positive_limit("UPLOAD_MAX_FILE_SIZE", FILE_LIMIT)
MAX_TOTAL_CHUNKS = positive_limit("UPLOAD_MAX_TOTAL_CHUNKS", 10000)
MULTIPART_ROOT = str(Path(UPLOAD_FOLDER) / ".multipart")


class FileUpdateData(TypedDict, total=False):
    """Partial file metadata update as present in the request."""

    name: str
    status: str
    parent_id: int


class BatchDeleteItemData(TypedDict, total=False):
    """One batch delete entry: the entity id and whether it is a folder."""

    id: int
    is_folder: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class GetFilesAndFoldersParams:
    """Directory listing filters, ordering and paging."""

    workspace_id: int
    parent_id: int | None
    page: int = 1
    page_size: int = 10
    name: str | None = None
    sort_by: str = "created_at"
    order: str = "desc"


def _generate_unique_filename(original_filename: str) -> str:
    """Return a collision-free object key derived from the original name."""
    extension = Path(original_filename).suffix
    base_name = original_filename[: len(original_filename) - len(extension)]
    safe_base_name = secure_filename(base_name)
    if safe_base_name:
        return f"{uuid.uuid4().hex}_{safe_base_name}{extension}"
    return f"{uuid.uuid4().hex}{extension}"


def _resolve_mime_type(path: str, provided: str | None = None) -> str | None:
    """Return the declared MIME type or guess it from the path extension."""
    if provided:
        return provided
    return mimetypes.guess_type(path)[0]


def _normalize_content_hash(content_hash: str | None) -> str | None:
    """Return the lowercase SHA-256 digest, rejecting malformed values."""
    if not content_hash:
        return None
    normalized = content_hash.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", normalized):
        message = "Invalid content_hash"
        raise BusinessRuleError(message)
    return normalized


def _calculate_file_hash(path: str) -> str:
    """Return the SHA-256 digest of the staged payload."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        while True:
            chunk = file_obj.read(COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _get_reusable_source_file(
    session: Session, content_hash: str, file_size: int, workspace_id: int,
) -> File | None:
    """Return a stored object that can satisfy the upload without a PUT."""
    return _upload_service().reusable_source(
        session, content_hash, file_size, workspace_id,
    )


def _persist_file_record(session: Session, spec: FileRecordSpec) -> File:
    """Insert the file row, stage its index task and commit the transaction."""
    parent_id = spec.parent_id
    if parent_id is not None:
        parent_id = _resolve_upload_parent_id(session, spec.workspace_id, parent_id)
    new_file = File(
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
    repository.insert_file(session, new_file)
    if spec.status != "success":
        stage_file_tasks(session, [new_file.id])
    if spec.before_commit is not None:
        spec.before_commit(new_file)
    session.commit()
    return new_file


def _log_file_created(file_obj: File) -> None:
    """Record the creation event of a published file."""
    workspace_id = file_obj.workspace_id
    if not workspace_id:
        return
    change_log_service.log_event(
        change_log_service.LogEventParams(
            workspace_id=workspace_id,
            entity_type="file",
            entity_id=file_obj.id,
            action="create",
            old_parent_id=None,
            new_parent_id=file_obj.parent_id,
            old_name=None,
            new_name=file_obj.name,
        ),
    )


def _clone_existing_file(session: Session, params: CloneSourceParams) -> File:
    """Instant upload: reuse the stored object path for a new file row."""
    return _upload_service().clone_source(session, params)


def preflight_file_upload(
    session: Session, workspace_id: int, uploader_id: int, data: PreflightData,
) -> dict[str, object]:
    """Clone a stored object when the hash matches; skip the chunk transfer."""
    return _upload_service().preflight_file_upload(
        session, workspace_id, uploader_id, data,
    )


def _invalidate_processing_cache(
    file_ids: list[int], workspace_id: int | None,
) -> None:
    """Invalidate views; task deliveries already belong to the file transaction."""
    if file_ids and workspace_id:
        clear_search_cache(workspace_id)


def _resolve_upload_parent_id(
    session: Session, workspace_id: int, parent_id: int | None,
) -> int | None:
    """Resolve the workspace root and reject parents from another workspace."""
    if parent_id is None:
        return folder_service.get_root_folder_id(session, workspace_id)

    parent = repository.find_folder(session, parent_id)
    if not parent or parent.workspace_id != workspace_id:
        message = "Parent folder belongs to another workspace"
        raise PermissionDeniedError(message)
    return parent_id


def multipart_upload_dir(workspace_id: int, upload_id: str) -> str:
    """Return the multipart session directory of the workspace."""
    return multipart_storage.upload_directory(MULTIPART_ROOT, workspace_id, upload_id)


def multipart_chunks_dir(upload_dir: str) -> str:
    """Return the chunk directory inside a multipart session directory."""
    return multipart_storage.chunks_directory(upload_dir)


def multipart_meta_path(upload_dir: str) -> str:
    """Return the metadata file path inside a multipart session directory."""
    return multipart_storage.metadata_path(upload_dir)


def safe_upload_id(upload_id: str) -> str:
    """Return the upload id after validating it against the safe charset."""
    return validate_upload_id(upload_id)


def list_uploaded_chunks(chunks_dir: str) -> list[int]:
    """Return the indexes of the chunks already stored for a session."""
    return multipart_storage.list_chunks(chunks_dir)


def load_multipart_meta(workspace_id: int, upload_id: str) -> dict[str, Any]:
    """Read and workspace-check the metadata of an existing session."""
    validated_upload_id = safe_upload_id(upload_id)
    upload_dir = multipart_upload_dir(workspace_id, validated_upload_id)
    meta_path = multipart_meta_path(upload_dir)
    if not Path(meta_path).exists():
        message = "Upload session not found"
        raise ResourceNotFoundError(message)
    try:
        meta = multipart_storage.read_metadata(meta_path, root=MULTIPART_ROOT)
    except Exception as e:
        logger.error("Failed to read upload metadata (error_type={})", type(e).__name__)
        message = "Upload session corrupted"
        raise ServiceOperationError(message) from e

    if int(meta.get("workspace_id", -1)) != workspace_id:
        message = "Permission denied"
        raise PermissionDeniedError(message)
    return meta


def _write_multipart_meta(
    meta_path: str,
    meta: Mapping[str, object],
    root: str | None = None,
) -> None:
    """Persist session metadata through the shared multipart storage helper."""
    multipart_storage.write_metadata(meta_path, dict(meta), root=root)


def _upload_service() -> FileUploadService:
    """Build the upload service over the module-level publication ports."""
    return FileUploadService(
        UploadSettings(UPLOAD_FOLDER, MAX_UPLOAD_SIZE),
        FilePublicationOperations(
            object_name=_generate_unique_filename,
            mime_type=_resolve_mime_type,
            hash_file=_calculate_file_hash,
            persist_record=_persist_file_record,
            created=_log_file_created,
            journal_batch=change_log_service.log_events_batch,
            invalidate=_invalidate_processing_cache,
            normalize_hash=_normalize_content_hash,
            resolve_parent=_resolve_upload_parent_id,
            clear_search=clear_search_cache,
        ),
        storage_factory=get_storage_client,
        staging_factory=StagedUpload,
        write_upload=save_bounded,
    )


def create_file(
    session: Session,
    file_obj: UploadSource,
    data: UploadData,
    *,
    before_commit: Callable[[File], None] | None = None,
    authorize_upload: Callable[[], None] | None = None,
) -> File:
    """Store one bounded upload and publish its file row."""
    return _upload_service().create_file(
        session,
        file_obj,
        data,
        before_commit=before_commit,
        authorize_upload=authorize_upload,
    )


def create_uploaded_file(
    session: Session,
    workspace_id: int,
    uploader_id: int,
    upload: UploadSource,
    parent_id: int | None = None,
) -> File:
    """Upload one file: default to the workspace root and require a filename."""
    if not upload.filename:
        message = "No selected file"
        raise BusinessRuleError(message)
    resolved_parent = _resolve_upload_parent_id(session, workspace_id, parent_id)
    return create_file(
        session,
        upload,
        {
            "workspace_id": workspace_id,
            "uploader_id": uploader_id,
            "parent_id": resolved_parent,
        },
    )


def batch_create_files(
    session: Session, file_objs: list[UploadSource], data: BatchUploadData,
) -> list[File]:
    """Store every valid upload and publish the rows in one transaction."""
    return _upload_service().batch_create_files(session, file_objs, data)


def create_uploaded_files(
    session: Session,
    workspace_id: int,
    uploader_id: int,
    uploads: list[UploadSource],
    parent_id: int | None = None,
) -> list[File]:
    """Upload several files: default to the workspace root and require a name."""
    valid_uploads = [upload for upload in uploads if upload.filename]
    if not valid_uploads:
        message = "No selected files"
        raise BusinessRuleError(message)
    resolved_parent = _resolve_upload_parent_id(session, workspace_id, parent_id)
    return batch_create_files(
        session,
        valid_uploads,
        {
            "workspace_id": workspace_id,
            "uploader_id": uploader_id,
            "parent_id": resolved_parent,
        },
    )


def _multipart_service() -> MultipartUploadService:
    """Build the multipart service over the module-level publication ports."""
    return MultipartUploadService(
        MultipartSettings(
            UPLOAD_FOLDER,
            MULTIPART_ROOT,
            DEFAULT_CHUNK_SIZE,
            MAX_CHUNK_SIZE,
            MAX_UPLOAD_SIZE,
            MAX_TOTAL_CHUNKS,
        ),
        MultipartFileOperations(
            resolve_parent=_resolve_upload_parent_id,
            normalize_hash=_normalize_content_hash,
            reusable_source=_get_reusable_source_file,
            clone_source=_clone_existing_file,
            object_name=_generate_unique_filename,
            hash_file=_calculate_file_hash,
            mime_type=_resolve_mime_type,
            created=_log_file_created,
            invalidate=_invalidate_processing_cache,
        ),
        storage_factory=get_storage_client,
        write_metadata=_write_multipart_meta,
    )


def init_multipart_upload(
    session: Session, workspace_id: int, uploader_id: int, data: MultipartInitData,
) -> dict[str, object]:
    """Open (or reuse) the multipart session that will receive the chunks."""
    return _multipart_service().init_multipart_upload(
        session, workspace_id, uploader_id, data,
    )


def get_multipart_upload_status(
    session: Session, workspace_id: int, uploader_id: int, upload_id: str,
) -> dict[str, object]:
    """Return the recorded chunk progress of an open session."""
    return _multipart_service().get_multipart_upload_status(
        session, workspace_id, uploader_id, upload_id,
    )


def save_multipart_chunk(
    session: Session, params: SaveMultipartChunkParams,
) -> dict[str, object]:
    """Store one chunk inside the still-held reservation locks."""
    return _multipart_service().save_multipart_chunk(session, params)


def complete_multipart_upload(
    session: Session, workspace_id: int, uploader_id: int, upload_id: str,
) -> File:
    """Assemble the chunks, then publish the merged object and its row."""
    return _multipart_service().complete_multipart_upload(
        session, workspace_id, uploader_id, upload_id,
    )


def abort_multipart_upload(
    session: Session, workspace_id: int, uploader_id: int, upload_id: str,
) -> None:
    """Cancel the session reservation and delete its chunk directory."""
    _multipart_service().abort_multipart_upload(
        session, workspace_id, uploader_id, upload_id,
    )


def get_file(session: Session, file_id: int) -> File:
    """Return one file row or raise when it does not exist."""
    file_obj = repository.find_file(session, file_id)
    if not file_obj:
        message = "File not found"
        raise ResourceNotFoundError(message)
    return file_obj


def get_authorized_file(
    session: Session, workspace_id: int, user_id: int, file_id: int,
) -> File:
    """Check workspace membership and that the file belongs to the workspace.

    ``user_id`` is intentionally used for membership, never as a workspace ID
    and never as an uploader-ownership check.
    """
    assert_member(session, workspace_id, user_id)
    file_obj = get_file(session, file_id)
    if file_obj.workspace_id != workspace_id:
        message = "Permission denied"
        raise PermissionDeniedError(message)
    return file_obj


def get_downloadable_file(
    session: Session, workspace_id: int, user_id: int, file_id: int,
) -> File:
    """Return an authorized file whose object still exists in storage."""
    file_obj = get_authorized_file(session, workspace_id, user_id, file_id)
    storage = get_storage_client()
    if not storage.file_exists(file_obj.file_path):
        message = "File not found on server"
        raise ResourceNotFoundError(message)
    return file_obj


def get_files_and_folders(
    session: Session, params: GetFilesAndFoldersParams,
) -> dict[str, object]:
    """Return one directory page with its folders and file page."""
    page = max(params.page, 1)
    rows = repository.list_directory(
        session,
        ListDirectoryParams(
            workspace_id=params.workspace_id,
            parent_id=params.parent_id,
            page=page,
            page_size=params.page_size,
            name=params.name,
            sort_by=params.sort_by,
            order=params.order,
        ),
    )
    pages = (
        (rows.total + params.page_size - 1) // params.page_size
        if params.page_size > 0
        else 1
    )
    return {
        "folders": [folder.to_dict() for folder in rows.folders],
        "files": {
            "items": [file.to_dict() for file in rows.files],
            "total": rows.total,
            "page": page,
            "page_size": params.page_size,
            "pages": pages,
        },
    }


def update_file(session: Session, file_id: int, data: FileUpdateData) -> File:
    """Apply the partial metadata update and record the file change event."""
    file_obj = repository.find_file(session, file_id)
    if not file_obj:
        message = "File not found"
        raise ResourceNotFoundError(message)

    old_name = file_obj.name
    old_parent_id = file_obj.parent_id

    target_parent = data.get("parent_id")
    if target_parent is not None:
        target_folder = repository.find_folder(session, target_parent)
        if not target_folder or target_folder.workspace_id != file_obj.workspace_id:
            message = "Parent folder belongs to another workspace"
            raise PermissionDeniedError(message)

    if "name" in data:
        file_obj.name = data["name"]
    if "status" in data:
        file_obj.status = data["status"]
    if "parent_id" in data:
        file_obj.parent_id = data["parent_id"]
    session.commit()

    name_changed = file_obj.name != old_name
    parent_changed = file_obj.parent_id != old_parent_id
    if name_changed or parent_changed:
        action = "update_meta"
        if name_changed and not parent_changed:
            action = "rename"
        elif parent_changed and not name_changed:
            action = "move"

        change_log_service.log_event(
            change_log_service.LogEventParams(
                workspace_id=file_obj.workspace_id,
                entity_type="file",
                entity_id=file_obj.id,
                action=action,
                old_parent_id=old_parent_id,
                new_parent_id=file_obj.parent_id,
                old_name=old_name,
                new_name=file_obj.name,
            ),
        )

    clear_search_cache(file_obj.workspace_id)
    return file_obj


def _root_file_cache_key(session: Session, workspace_id: int) -> str:
    """Return the root-file cache key for the current workspace tree revision."""
    revision = sql_cache_revision(session, f"workspace:tree:{workspace_id}")
    return f"{workspace_id}:sql:{revision}"


def _root_file_cache_allowed(session: Session, workspace_id: int) -> bool:
    """Use the shared SQL cache gate with the cached root-file signature."""
    return sql_cache_allowed(session, workspace_id)


@cacheable(
    CacheableParams(prefix=ROOT_FILE_CACHE_PREFIX, expire=CACHE_EXPIRATION),
    cache_allowed=_root_file_cache_allowed,
    key=_root_file_cache_key,
)
def get_root_file_id(session: Session, workspace_id: int) -> int | None:
    """Return the id of the first root-level file of the workspace."""
    root_file = repository.find_root_file(session, workspace_id)
    return root_file.id if root_file else None


def _avatar_service() -> AvatarUploadService:
    """Build the avatar service over account, folder, share and file ports."""
    return AvatarUploadService(
        AVATAR_LIMIT,
        AvatarOperations(
            authorize_account=user_service.authorize_account_mutation,
            create_folder=folder_service.create_folder,
            create_share=share_service.create_share_link,
            set_avatar=account_repository.set_avatar,
            upload_file=create_file,
            evict_profile=lambda user_id: evict_cache(
                user_service.USER_CACHE_PREFIX, user_id,
            ),
        ),
    )


def upload_avatar(
    session: Session,
    img_file: UploadSource,
    user_id: int,
    *,
    actor_id: int | None = None,
) -> dict[str, object]:
    """Store the avatar in the personal workspace and publish its URL."""
    return _avatar_service().upload(session, img_file, user_id, actor_id=actor_id)


def upload_avatar_for_user(
    session: Session,
    actor_id: int,
    actor_role: str,
    user_id: int,
    upload: UploadSource,
) -> dict[str, object]:
    """Upload an avatar after checking the actor may mutate the account."""
    if actor_id != user_id and actor_role != "admin":
        message = "Permission denied"
        raise PermissionDeniedError(message)
    return upload_avatar(session, upload, user_id, actor_id=actor_id)


def batch_delete_items(
    session: Session,
    workspace_id: int,
    user_id: int,
    items: Sequence[BatchDeleteItemData],
) -> None:
    """Delete every listed file or folder after a write-access check."""
    for item in items:
        assert_can_write_locked(session, workspace_id, user_id)
        if "id" not in item:
            message = "Item id is required"
            raise BusinessRuleError(message)
        item_id = item["id"]
        if item.get("is_folder", False):
            folder_service.get_authorized_folder(
                session, workspace_id, user_id, item_id,
            )
            folder_service.delete_folder(session, item_id)
        else:
            get_authorized_file(session, workspace_id, user_id, item_id)
            delete_file(session, item_id)


def retry_embedding(session: Session, file_id: int) -> None:
    """Re-queue one failed file for indexing."""
    file_obj = repository.find_file(session, file_id)
    if not file_obj:
        return
    file_obj.status = "pending"
    stage_file_tasks(session, [file_obj.id])
    session.commit()


def rebuild_failed_indexes(session: Session, workspace_id: int | None = None) -> int:
    """Re-queue every failed file of the workspace and commit once."""
    try:
        file_ids = repository.failed_file_ids(session, workspace_id)
        if not file_ids:
            return 0
        repository.mark_pending(session, file_ids)
        stage_file_tasks(session, file_ids)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error(
            "Failed to update file status to pending (error_type={})",
            type(exc).__name__,
        )
        if isinstance(exc, DomainError):
            raise
        message = "Failed to rebuild file indexes"
        raise ServiceOperationError(message) from exc

    return len(file_ids)


def get_all_files(session: Session, workspace_id: int) -> list[File]:
    """Return every file row of the workspace."""
    return repository.list_workspace_files(session, workspace_id)


async def process_status(
    session: Session | None,
    workspace_id: int,
    *,
    session_factory: SessionFactory | None = None,
    user_id: int | None = None,
) -> dict[str, int]:
    """Aggregate indexing status on the thread that owns the SQL Session."""

    def work() -> dict[str, int]:
        if session_factory is not None:
            with session_factory() as owned_session:
                if user_id is not None:
                    assert_member(owned_session, workspace_id, user_id)
                return _process_status(owned_session, workspace_id)
        if session is None:
            message = "A session is required when no session_factory is provided"
            raise ServiceOperationError(message)
        return _process_status(session, workspace_id)

    return await run_owned(work)


def _process_status(session: Session, workspace_id: int) -> dict[str, int]:
    """Group the workspace files by status instead of loading every row."""
    cnt = repository.status_counts(session, workspace_id)
    return {
        "处理中": cnt.get("pending", 0) + cnt.get("processing", 0),
        "成功": cnt.get("success", 0),
        "失败": cnt.get("fail", 0),
    }


def cleanup_expired_uploads(
    max_age_hours: int = 24, *, session: Session | None = None,
) -> int:
    """Remove expired staging/multipart directories and close stale sessions."""
    count = cleanup_staging(UPLOAD_FOLDER, upload_quota.TTL_SECONDS)
    count += (
        upload_quota.cleanup(
            session,
            MULTIPART_ROOT,
            upload_root=UPLOAD_FOLDER,
            storage_factory=get_storage_client,
        )
        if session is not None
        else 0
    )
    active_ids: set[tuple[str, str]] = (
        upload_quota.active_reservation_ids(session) if session is not None else set()
    )
    return count + cleanup_legacy_multipart(
        MULTIPART_ROOT, max_age_hours * 3600, active_ids,
    )
