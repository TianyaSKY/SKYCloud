"""文件上传/下载及分片上传会话管理，兼容导出独立检索服务。

含秒传（content_hash 复用）、Bloom 预鉴权、物理文件引用计数删除等边界。
存储后端已迁移至 MinIO 对象存储（兼容 S3 API）。
"""

from app.infra.owned_work import run_owned
import hashlib
import mimetypes
import os
import re
import uuid
from typing import Any, Callable, cast

from werkzeug.utils import secure_filename

from app.exceptions import (
    BusinessRuleError,
    ConflictError,
    DomainError,
    PayloadTooLargeError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ServiceOperationError,
)
from sqlalchemy.orm import Session

from app.infra.extensions import UPLOAD_FOLDER
from app.infra.cache import (evict_cache_pattern, invalidate_cache_generation,
                             cacheable, sql_cache_allowed, sql_cache_revision)
from app.infra.file_task_outbox import stage_file_tasks
from app.infra.file_object_deletion import stage_object_deletion_fallback, lock_object_path
from app.infra.storage import get_storage_client
from app.infra import multipart_storage
from app.infra.multipart_cleanup import cleanup_legacy_multipart
from app.infra.upload_adapter import save_bounded
from app.infra.upload_staging import StagedUpload, cleanup_staging, close_staging_safely
from app.infra.upload_limits import FILE_LIMIT, AVATAR_LIMIT, positive_limit
from app.models.file import File
from app.features.folder import change_log as change_log_service
from app.features.workspace.permissions import assert_member, assert_can_write_locked
from app.features.file import upload_authorization
from app.features.file import upload_quota, repository
from app.features.file.upload_service import FileUploadService, UploadSettings, FilePublicationOperations
from app.features.file.avatar_service import AvatarUploadService, AvatarOperations
from app.features.file.multipart_service import (
    MultipartUploadService, MultipartSettings, MultipartFileOperations, validate_upload_id,
)
from app.features.file.upload_recovery import compensate_failed_uploads, _release_failed_upload
from app.features.file.repository import escape_like as _escape_like
from app.features.file.search_service import (
    search_files, embedding_desc, _search_files_fuzzy, _search_files_vector_by_embedding,
    SEARCH_CACHE_PREFIX,
)

from loguru import logger

CACHE_EXPIRATION = 3600
ROOT_FILE_CACHE_PREFIX = "workspace:root:file:v2"

COPY_BUFFER_SIZE = 1024 * 1024

DEFAULT_CHUNK_SIZE = 2 * 1024 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024
MAX_UPLOAD_SIZE = FILE_LIMIT
MAX_TOTAL_CHUNKS = 10000
MULTIPART_ROOT = os.path.join(UPLOAD_FOLDER, ".multipart")


DEFAULT_CHUNK_SIZE = positive_limit("UPLOAD_CHUNK_SIZE", DEFAULT_CHUNK_SIZE)
MAX_CHUNK_SIZE = positive_limit("UPLOAD_MAX_CHUNK_SIZE", MAX_CHUNK_SIZE)
MAX_UPLOAD_SIZE = positive_limit("UPLOAD_MAX_FILE_SIZE", MAX_UPLOAD_SIZE)
MAX_TOTAL_CHUNKS = positive_limit("UPLOAD_MAX_TOTAL_CHUNKS", MAX_TOTAL_CHUNKS)


def _generate_unique_filename(original_filename: str) -> str:
    base_name, extension = os.path.splitext(original_filename)
    safe_base_name = secure_filename(base_name)
    if safe_base_name:
        return f"{uuid.uuid4().hex}_{safe_base_name}{extension}"
    return f"{uuid.uuid4().hex}{extension}"


def _resolve_mime_type(path: str, provided: str | None = None) -> str | None:
    if provided:
        return provided
    return mimetypes.guess_type(path)[0]


def _normalize_content_hash(content_hash: str | None) -> str | None:
    if not content_hash:
        return None
    normalized = content_hash.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", normalized):
        raise BusinessRuleError("Invalid content_hash")
    return normalized


def _calculate_file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        while True:
            chunk = file_obj.read(COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _get_reusable_source_file(session: Session, content_hash: str, file_size: int, workspace_id: int) -> File | None:
    return _upload_service().reusable_source(session, content_hash, file_size, workspace_id)


def _persist_file_record(
        session: Session,
        *,
        name: str,
        file_path: str,
        file_size: int,
        mime_type: str | None,
        workspace_id: int,
        uploader_id: int | None,
        parent_id: int | None,
        content_hash: str | None,
        status: str = "pending",
        description: str | None = None,
        vector_info: Any = None,
        before_commit: Callable[[File], None] | None = None,
) -> File:
    if parent_id is not None:
        parent_id = _resolve_upload_parent_id(session, workspace_id, parent_id)
    new_file = File(
        name=name,
        file_path=file_path,
        file_size=file_size,
        mime_type=mime_type,
        workspace_id=workspace_id,
        uploader_id=uploader_id,
        parent_id=parent_id,
        content_hash=content_hash,
        status=status,
        description=description,
        vector_info=vector_info,
    )
    repository.insert_file(session, new_file)
    if status != "success":
        stage_file_tasks(session, [new_file.id])
    if before_commit is not None:
        before_commit(new_file)
    session.commit()
    return new_file


def _log_file_created(file_obj: File) -> None:
    workspace_id = cast(int | None, file_obj.workspace_id)
    if not workspace_id:
        return
    change_log_service.log_event(
        workspace_id=workspace_id,
        entity_type="file",
        entity_id=cast(int, file_obj.id),
        action="create",
        old_parent_id=None,
        new_parent_id=cast(int | None, file_obj.parent_id),
        old_name=None,
        new_name=cast(str | None, file_obj.name),
    )


def _clone_existing_file(session: Session, source_file: File, *, filename: str, workspace_id: int,
                         uploader_id: int, parent_id: int | None, mime_type: str | None, content_hash: str) -> File:
    return _upload_service().clone_source(session, source_file, filename=filename, workspace_id=workspace_id,
                                          uploader_id=uploader_id, parent_id=parent_id,
                                          mime_type=mime_type, content_hash=content_hash)


def preflight_file_upload(session: Session, workspace_id: int, uploader_id: int, data: dict[str, Any]) -> dict[str, Any]:
    return _upload_service().preflight_file_upload(session, workspace_id, uploader_id, data)


def _invalidate_processing_cache(file_ids: list[int], workspace_id: int | None) -> None:
    """Invalidate views; task deliveries already belong to the file transaction."""
    if file_ids and workspace_id:
        _clear_search_cache(workspace_id)


def _resolve_upload_parent_id(
        session: Session, workspace_id: int, parent_id: int | None
) -> int | None:
    """Resolve the root and reject parent folders from another workspace."""

    if parent_id is None:
        from app.features.folder import service as folder_service

        return folder_service.get_root_folder_id(session, workspace_id)

    parent = repository.find_folder(session, parent_id)
    if not parent or parent.workspace_id != workspace_id:
        raise PermissionDeniedError("Parent folder belongs to another workspace")
    return parent_id


def _multipart_upload_dir(workspace_id: int, upload_id: str) -> str:
    return multipart_storage.upload_directory(MULTIPART_ROOT, workspace_id, upload_id)


def _multipart_chunks_dir(upload_dir: str) -> str:
    return multipart_storage.chunks_directory(upload_dir)


def _multipart_meta_path(upload_dir: str) -> str:
    return multipart_storage.metadata_path(upload_dir)


def _safe_upload_id(upload_id: str) -> str:
    return validate_upload_id(upload_id)


def _list_uploaded_chunks(chunks_dir: str) -> list[int]:
    return multipart_storage.list_chunks(chunks_dir)


def _load_multipart_meta(workspace_id: int, upload_id: str) -> dict[str, Any]:
    safe_upload_id = _safe_upload_id(upload_id)
    upload_dir = _multipart_upload_dir(workspace_id, safe_upload_id)
    meta_path = _multipart_meta_path(upload_dir)
    if not os.path.exists(meta_path):
        raise ResourceNotFoundError("Upload session not found")
    try:
        meta = multipart_storage.read_metadata(meta_path, root=MULTIPART_ROOT)
    except Exception as e:
        logger.error("Failed to read upload metadata (error_type={})", type(e).__name__)
        raise ServiceOperationError("Upload session corrupted")

    if int(meta.get("workspace_id", -1)) != workspace_id:
        raise PermissionDeniedError("Permission denied")
    return meta


def _write_multipart_meta(meta_path: str, meta: dict[str, Any], *, root: str | None = None) -> None:
    multipart_storage.write_metadata(meta_path, meta, root=root)


def _upload_service() -> FileUploadService:
    return FileUploadService(
        UploadSettings(UPLOAD_FOLDER, MAX_UPLOAD_SIZE),
        FilePublicationOperations(
            object_name=_generate_unique_filename, mime_type=_resolve_mime_type,
            hash_file=_calculate_file_hash, persist_record=_persist_file_record,
            created=_log_file_created, journal_batch=change_log_service.log_events_batch,
            invalidate=_invalidate_processing_cache, normalize_hash=_normalize_content_hash,
            resolve_parent=_resolve_upload_parent_id, clear_search=_clear_search_cache,
        ),
        storage_factory=get_storage_client, staging_factory=StagedUpload,
        write_upload=save_bounded,
    )


def create_file(session: Session, file_obj: Any, data: dict[str, Any], *,
                before_commit: Callable[[File], None] | None = None,
                authorize_upload: Callable[[], None] | None = None) -> File:
    return _upload_service().create_file(session, file_obj, data,
                                        before_commit=before_commit, authorize_upload=authorize_upload)


def create_uploaded_file(
        session: Session,
        workspace_id: int,
        uploader_id: int,
        upload: Any,
        parent_id: int | None = None,
) -> File:
    """单文件上传：默认挂到工作空间根目录，并校验文件名非空。"""
    if not getattr(upload, "filename", None):
        raise BusinessRuleError("No selected file")
    parent_id = _resolve_upload_parent_id(session, workspace_id, parent_id)
    return create_file(session, upload, {"workspace_id": workspace_id, "uploader_id": uploader_id, "parent_id": parent_id})


def batch_create_files(session: Session, file_objs: list[Any], data: dict[str, Any]) -> list[File]:
    return _upload_service().batch_create_files(session, file_objs, data)


def create_uploaded_files(
        session: Session,
        workspace_id: int, uploader_id: int, uploads: list[Any], parent_id: int | None = None
) -> list[File]:
    valid_uploads = [upload for upload in uploads if getattr(upload, "filename", None)]
    if not valid_uploads:
        raise BusinessRuleError("No selected files")
    parent_id = _resolve_upload_parent_id(session, workspace_id, parent_id)
    return batch_create_files(
        session, valid_uploads, {"workspace_id": workspace_id, "uploader_id": uploader_id, "parent_id": parent_id}
    )


def _multipart_service() -> MultipartUploadService:
    return MultipartUploadService(
        MultipartSettings(UPLOAD_FOLDER, MULTIPART_ROOT, DEFAULT_CHUNK_SIZE,
                          MAX_CHUNK_SIZE, MAX_UPLOAD_SIZE, MAX_TOTAL_CHUNKS),
        MultipartFileOperations(
            resolve_parent=_resolve_upload_parent_id, normalize_hash=_normalize_content_hash,
            reusable_source=_get_reusable_source_file, clone_source=_clone_existing_file,
            object_name=_generate_unique_filename, hash_file=_calculate_file_hash,
            mime_type=_resolve_mime_type, created=_log_file_created,
            invalidate=_invalidate_processing_cache,
        ),
        storage_factory=get_storage_client, write_metadata=_write_multipart_meta,
    )


def init_multipart_upload(session: Session, workspace_id: int, uploader_id: int, data: dict[str, Any]) -> dict[str, Any]:
    return _multipart_service().init_multipart_upload(session, workspace_id, uploader_id, data)


def get_multipart_upload_status(session: Session, workspace_id: int, uploader_id: int, upload_id: str) -> dict[str, Any]:
    return _multipart_service().get_multipart_upload_status(session, workspace_id, uploader_id, upload_id)


def save_multipart_chunk(session: Session, uploader_id: int, workspace_id: int,
                         upload_id: str, chunk_index: int, chunk_obj: Any) -> dict[str, Any]:
    return _multipart_service().save_multipart_chunk(session, uploader_id, workspace_id, upload_id, chunk_index, chunk_obj)


def complete_multipart_upload(session: Session, workspace_id: int, uploader_id: int, upload_id: str) -> File:
    return _multipart_service().complete_multipart_upload(session, workspace_id, uploader_id, upload_id)


def abort_multipart_upload(session: Session, workspace_id: int, uploader_id: int, upload_id: str) -> None:
    _multipart_service().abort_multipart_upload(session, workspace_id, uploader_id, upload_id)


def get_file(session: Session, id: int) -> File:
    file_obj = repository.find_file(session, id)
    if not file_obj:
        raise ResourceNotFoundError("File not found")
    return file_obj


def get_authorized_file(session: Session, workspace_id: int, user_id: int, file_id: int) -> File:
    """校验用户为成员且文件属于当前工作空间.

    ``user_id`` is intentionally used for membership, never as a workspace ID
    and never as an uploader-ownership check.
    """
    assert_member(session, workspace_id, user_id)
    file_obj = get_file(session, file_id)
    # 校验文件属于当前工作空间
    if file_obj.workspace_id != workspace_id:
        raise PermissionDeniedError("Permission denied")
    return file_obj


def get_downloadable_file(session: Session, workspace_id: int, user_id: int, file_id: int) -> File:
    file_obj = get_authorized_file(session, workspace_id, user_id, file_id)
    storage = get_storage_client()
    if not storage.file_exists(cast(str, file_obj.file_path)):
        raise ResourceNotFoundError("File not found on server")
    return file_obj


def get_files_and_folders(
        session: Session,
        workspace_id: int,
        parent_id: int | None,
        page: int = 1,
        page_size: int = 10,
        name: str | None = None,
        sort_by: str = "created_at",
        order: str = "desc",
) -> dict[str, Any]:
    if page < 1:
        page = 1

    rows = repository.list_directory(session, workspace_id, parent_id, page, page_size, name, sort_by, order)
    return {
        "folders": [folder.to_dict() for folder in rows.folders],
        "files": {
            "items": [file.to_dict() for file in rows.files], "total": rows.total,
            "page": page, "page_size": page_size,
            "pages": (rows.total + page_size - 1) // page_size if page_size > 0 else 1,
        },
    }


def update_file(session: Session, id: int, data: dict[str, Any]) -> File:
    file_obj = repository.find_file(session, id)
    if not file_obj:
        raise ResourceNotFoundError("File not found")

    old_name = file_obj.name
    old_parent_id = file_obj.parent_id

    if "parent_id" in data and data["parent_id"] is not None:
        target_folder = repository.find_folder(session, data["parent_id"])
        if not target_folder or target_folder.workspace_id != file_obj.workspace_id:
            raise PermissionDeniedError("Parent folder belongs to another workspace")

    file_obj.name = data.get("name", file_obj.name)
    file_obj.status = data.get("status", file_obj.status)
    file_obj.parent_id = data.get("parent_id", file_obj.parent_id)
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
            workspace_id=file_obj.workspace_id,
            entity_type="file",
            entity_id=file_obj.id,
            action=action,
            old_parent_id=old_parent_id,
            new_parent_id=file_obj.parent_id,
            old_name=old_name,
            new_name=file_obj.name,
        )

    _clear_search_cache(file_obj.workspace_id)
    return file_obj


def delete_file(session: Session, id: int, commit: bool = True, log_event: bool = True) -> None:
    """原子删除文件记录并暂存对象清理意图；Worker 在无共享引用时删除对象。

    commit/log_event=False 供递归批量删除时由外层统一提交。
    """
    file_obj = repository.find_file(session, id)
    if not file_obj:
        raise ResourceNotFoundError("File not found")

    uploader_id = file_obj.uploader_id
    old_parent_id = file_obj.parent_id
    old_name = file_obj.name
    entity_id = file_obj.id
    object_name = cast(str, file_obj.file_path)

    stage_object_deletion_fallback(session, object_name)
    repository.remove_file(session, file_obj)
    if commit:
        session.commit()
        if log_event:
            change_log_service.log_event(
                workspace_id=file_obj.workspace_id,
                entity_type="file",
                entity_id=entity_id,
                action="delete",
                old_parent_id=old_parent_id,
                new_parent_id=None,
                old_name=old_name,
                new_name=None,
            )
        _clear_search_cache(file_obj.workspace_id)


def _clear_search_cache(workspace_id: int | None) -> None:
    if workspace_id is not None:
        invalidate_cache_generation(f"cache:generation:search:fuzzy:{workspace_id}")
        evict_cache_pattern(f"{SEARCH_CACHE_PREFIX}:{workspace_id}")


@cacheable(
    prefix=ROOT_FILE_CACHE_PREFIX,
    expire=CACHE_EXPIRATION,
    cache_allowed=sql_cache_allowed,
    key=lambda session, workspace_id, **_: f"{workspace_id}:sql:{sql_cache_revision(session, f'workspace:tree:{workspace_id}')}",
)
def get_root_file_id(session: Session, workspace_id: int) -> int | None:
    root_file = repository.find_root_file(session, workspace_id)
    return root_file.id if root_file else None


def _avatar_service() -> AvatarUploadService:
    from app.features.folder import service as folder_service
    from app.features.share import service as share_service
    from app.features.auth import user_service, account_repository
    from app.infra.cache import evict_cache
    return AvatarUploadService(AVATAR_LIMIT, AvatarOperations(
        authorize_account=user_service.authorize_account_mutation,
        create_folder=folder_service.create_folder,
        create_share=share_service.create_share_link,
        set_avatar=account_repository.set_avatar,
        upload_file=create_file,
        evict_profile=lambda user_id: evict_cache(user_service.USER_CACHE_PREFIX, user_id),
    ))


def upload_avatar(session: Session, img_file: Any, user_id: int, *, actor_id: int | None = None) -> dict[str, Any]:
    return _avatar_service().upload(session, img_file, user_id, actor_id=actor_id)


def upload_avatar_for_user(
        session: Session,
        actor_id: int, actor_role: str, user_id: int, upload: Any
) -> dict[str, Any]:
    if actor_id != user_id and actor_role != "admin":
        raise PermissionDeniedError("Permission denied")
    return upload_avatar(session, upload, user_id, actor_id=actor_id)


def batch_delete_items(session: Session, workspace_id: int, user_id: int, items: list[dict[str, Any]]) -> None:
    from app.features.folder import service as folder_service

    for item in items:
        assert_can_write_locked(session, workspace_id, user_id)
        item_id = item.get("id")
        if item_id is None:
            raise BusinessRuleError("Item id is required")
        is_folder = item.get("is_folder", False)
        if is_folder:
            folder_service.get_authorized_folder(session, workspace_id, user_id, item_id)
            folder_service.delete_folder(session, item_id)
        else:
            get_authorized_file(session, workspace_id, user_id, item_id)
            delete_file(session, item_id)


def retry_embedding(session: Session, file_id: int) -> None:
    file_obj = repository.find_file(session, file_id)
    if not file_obj:
        return
    file_obj.status = "pending"
    stage_file_tasks(session, [file_obj.id])
    session.commit()


def rebuild_failed_indexes(session: Session, workspace_id: int | None = None) -> int:
    try:
        file_ids = repository.failed_file_ids(session, workspace_id)
        if not file_ids:
            return 0
        repository.mark_pending(session, file_ids)
        stage_file_tasks(session, file_ids)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error("Failed to update file status to pending (error_type={})", type(exc).__name__)
        if isinstance(exc, DomainError):
            raise
        raise ServiceOperationError("Failed to rebuild file indexes") from exc

    return len(file_ids)


def get_all_files(session: Session, workspace_id: int) -> list[File]:
    return repository.list_workspace_files(session, workspace_id)


async def process_status(session: Session | None, workspace_id: int, *, session_factory=None,
                         user_id: int | None = None) -> dict[str, int]:
    def work():
        if session_factory is not None:
            with session_factory() as owned_session:
                if user_id is not None:
                    assert_member(owned_session, workspace_id, user_id)
                return _process_status(owned_session, workspace_id)
        if session is None:
            raise ServiceOperationError("A session is required when no session_factory is provided")
        return _process_status(session, workspace_id)
    return await run_owned(work)


def _process_status(session: Session, workspace_id: int) -> dict[str, int]:
    """按状态聚合统计；用 GROUP BY 避免把空间全部文件载入内存。"""
    cnt = repository.status_counts(session, workspace_id)
    return {
        "处理中": cnt.get("pending", 0) + cnt.get("processing", 0),
        "成功": cnt.get("success", 0),
        "失败": cnt.get("fail", 0),
    }


def cleanup_expired_uploads(max_age_hours: int = 24, *, session: Session | None = None) -> int:
    """清理超时未完成的分片上传临时目录，回收磁盘。"""
    count = cleanup_staging(UPLOAD_FOLDER, upload_quota.TTL_SECONDS)
    count += upload_quota.cleanup(session, MULTIPART_ROOT, upload_root=UPLOAD_FOLDER, storage_factory=get_storage_client) if session is not None else 0
    active_ids = upload_quota.active_reservation_ids(session) if session is not None else set()
    return count + cleanup_legacy_multipart(MULTIPART_ROOT, max_age_hours * 3600, active_ids)
