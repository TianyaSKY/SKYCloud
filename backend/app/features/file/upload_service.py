"""Single and batch upload orchestration with explicit publication/byte ports.

The caller owns the SQL Session; durable reservations cover each object PUT.
Batch failures compensate every accepted object before reporting the result.
"""
from dataclasses import dataclass
import os
from typing import Any, Callable, cast

from loguru import logger
from sqlalchemy.orm import Session
from app.exceptions import BusinessRuleError, ConflictError, DomainError, PayloadTooLargeError, ServiceOperationError
from app.features.file import repository, upload_authorization
from app.features.file.upload_recovery import compensate_failed_uploads
from app.infra.file_task_outbox import stage_file_tasks
from app.infra.file_object_deletion import lock_object_path
from app.features.workspace.permissions import assert_can_write_locked
from app.infra.upload_adapter import save_bounded
from app.infra.upload_staging import StagedUpload, close_staging_safely
from app.models.file import File


@dataclass(frozen=True)
class UploadSettings:
    upload_root: str
    max_upload_size: int


@dataclass(frozen=True)
class FilePublicationOperations:
    object_name: Callable[[str], str]
    mime_type: Callable[[str, str | None], str | None]
    hash_file: Callable[[str], str]
    persist_record: Callable[..., File]
    created: Callable[[File], None]
    journal_batch: Callable[[int, list[dict[str, Any]]], None]
    invalidate: Callable[[list[int], int | None], None]
    normalize_hash: Callable[[str | None], str | None]
    resolve_parent: Callable[[Session, int, int | None], int | None]
    clear_search: Callable[[int | None], None]


class FileUploadService:
    def __init__(self, settings: UploadSettings, files: FilePublicationOperations, *,
                 storage_factory: Callable, staging_factory: Callable = StagedUpload,
                 write_upload: Callable = save_bounded):
        self.settings = settings
        self.files = files
        self.storage_factory = storage_factory
        self.staging_factory = staging_factory
        self.write_upload = write_upload

    def create_file(self, session: Session, file_obj: Any, data: dict[str, Any], *,
                    before_commit: Callable[[File], None] | None = None,
                    authorize_upload: Callable[[], None] | None = None) -> File:
        storage = self.storage_factory()
        # 分片临时目录仍用本地
        os.makedirs(self.settings.upload_root, exist_ok=True)

        original_filename = file_obj.filename or "unknown"
        unique_filename = self.files.object_name(original_filename)
        staging = self.staging_factory(self.settings.upload_root, original_filename)
        local_tmp_path = staging.path

        reservation_id = None
        try:
            self.write_upload(file_obj, local_tmp_path, data.get("max_bytes", self.settings.max_upload_size))
            file_size = os.path.getsize(local_tmp_path)
            mime_type = self.files.mime_type(local_tmp_path, getattr(file_obj, "mimetype", None))
            content_hash = self.files.hash_file(local_tmp_path)
            if authorize_upload is not None:
                authorize_upload()
            reservation_id = upload_authorization.reserve(session, data["workspace_id"], data["uploader_id"], file_size,
                reservation_id=staging.directory.name, details={"object_name": unique_filename, "stage_id": staging.directory.name}).id
            # 上传到 MinIO 对象存储
            storage.upload_file(local_tmp_path, unique_filename)

            if authorize_upload is not None:
                authorize_upload()
            upload_authorization.consume(session, data["workspace_id"], data["uploader_id"], reservation_id, file_size)
            new_file = self.files.persist_record(session,
                name=original_filename,
                file_path=unique_filename,
                file_size=file_size,
                mime_type=mime_type,
                workspace_id=data.get("workspace_id"),
                uploader_id=data.get("uploader_id"),
                parent_id=data.get("parent_id"),
                content_hash=content_hash,
                before_commit=before_commit,
            )
        except Exception as e:
            compensate_failed_uploads(session, data["workspace_id"], data["uploader_id"],
                                      [(reservation_id, unique_filename)], storage)
            logger.error("Failed to save uploaded file metadata (error_type={})", type(e).__name__)
            if isinstance(e, DomainError):
                raise
            raise ServiceOperationError("Failed to save file metadata")
        finally:
            # 清理本地临时文件
            close_staging_safely(staging)

        self.files.created(new_file)
        self.files.invalidate(
            [cast(int, new_file.id)], cast(int | None, new_file.workspace_id)
        )
        return new_file


    def batch_create_files(self,
            session: Session,
            file_objs: list[Any], data: dict[str, Any]
    ) -> list[File]:
        valid_files = [file_obj for file_obj in file_objs if file_obj and file_obj.filename]
        if not valid_files:
            return []

        storage = self.storage_factory()
        os.makedirs(self.settings.upload_root, exist_ok=True)

        new_files = []
        object_keys: list[str] = []
        reservations = []
        workspace_id = data.get("workspace_id")
        uploader_id = data.get("uploader_id")

        for file_obj in valid_files:
            staging = None
            try:
                original_filename = file_obj.filename
                unique_filename = self.files.object_name(original_filename)
                staging = self.staging_factory(self.settings.upload_root, original_filename)
                local_tmp_path = staging.path
                self.write_upload(file_obj, local_tmp_path, self.settings.max_upload_size)
                file_size = os.path.getsize(local_tmp_path)
                mime_type = self.files.mime_type(local_tmp_path, getattr(file_obj, "mimetype", None))
                content_hash = self.files.hash_file(local_tmp_path)

                reservation = upload_authorization.reserve(session, workspace_id, uploader_id, file_size,
                    reservation_id=staging.directory.name, details={"object_name": unique_filename, "stage_id": staging.directory.name})
                reservations.append(reservation.id)
                object_keys.append(unique_filename)
                storage.upload_file(local_tmp_path, unique_filename)
                new_file = File(
                    name=original_filename,
                    file_path=unique_filename,
                    file_size=file_size,
                    mime_type=mime_type,
                    content_hash=content_hash,
                    workspace_id=workspace_id,
                    uploader_id=uploader_id,
                    parent_id=data.get("parent_id"),
                )
                new_files.append(new_file)
            except Exception as exc:
                compensate_failed_uploads(session, workspace_id, uploader_id,
                                          zip(reservations, object_keys), storage)
                if isinstance(exc, DomainError):
                    raise
                raise ServiceOperationError("Failed to upload file") from exc
            finally:
                if staging is not None:
                    close_staging_safely(staging)

        try:
            for reservation_id, file_obj in zip(reservations, new_files):
                upload_authorization.consume(session, workspace_id, uploader_id, reservation_id, file_obj.file_size)
            repository.insert_files(session, new_files)
            stage_file_tasks(session, [file.id for file in new_files])
            session.commit()
        except Exception as exc:
            compensate_failed_uploads(session, workspace_id, uploader_id,
                                      zip(reservations, object_keys), storage)
            if isinstance(exc, DomainError):
                raise
            raise ServiceOperationError("Failed to save uploaded file metadata") from exc

        if uploader_id:
            self.files.journal_batch(
                workspace_id,
                [
                    {
                        "entity_type": "file",
                        "entity_id": cast(int, file_obj.id),
                        "action": "create",
                        "old_parent_id": None,
                        "new_parent_id": cast(int | None, file_obj.parent_id),
                        "old_name": None,
                        "new_name": cast(str | None, file_obj.name),
                    }
                    for file_obj in new_files
                ],
            )

        self.files.invalidate([cast(int, f.id) for f in new_files], workspace_id)
        return new_files


    def reusable_source(self, session: Session, content_hash: str, file_size: int) -> File | None:
        source_file = repository.find_reusable_file(session, content_hash, file_size)
        if not source_file:
            return None
        storage = self.storage_factory()
        if not storage.file_exists(cast(str, source_file.file_path)):
            return None
        return source_file


    def clone_source(self,
            session: Session,
            source_file: File,
            *,
            filename: str,
            workspace_id: int,
            uploader_id: int,
            parent_id: int | None,
            mime_type: str | None,
            content_hash: str,
    ) -> File:
        """秒传：复用已有物理文件路径；仅 success 状态才继承 description/vector。"""
        source_id = source_file.id
        object_name = cast(str, source_file.file_path)
        file_size = int(source_file.file_size or 0)

        reservation = upload_authorization.reserve(session, workspace_id, uploader_id, file_size, kind="instant")
        reservation_id = reservation.id
        try:
            # Identity locks precede the shared-object lock, matching cancellation.
            assert_can_write_locked(session, workspace_id, uploader_id)
            lock_object_path(session, object_name)
            source_file = repository.find_live_file(session, source_id)
            if source_file is None or source_file.file_path != object_name or not self.storage_factory().file_exists(object_name):
                raise ConflictError("Reusable file no longer available; retry upload")
            resolved_mime = mime_type or cast(str | None, source_file.mime_type)
            status = cast(str | None, source_file.status) or "pending"
            description = cast(str | None, source_file.description) if status == "success" else None
            vector_info = source_file.vector_info if status == "success" else None
            upload_authorization.consume(session, workspace_id, uploader_id, reservation_id, int(source_file.file_size or 0))
            new_file = self.files.persist_record(session,
                name=filename,
                file_path=cast(str, source_file.file_path),
                file_size=cast(int | None, source_file.file_size) or 0,
                mime_type=resolved_mime,
                workspace_id=workspace_id,
                uploader_id=uploader_id,
                parent_id=parent_id,
                content_hash=content_hash,
                status=status,
                description=description,
                vector_info=vector_info,
            )
        except Exception as e:
            compensate_failed_uploads(session, workspace_id, uploader_id,
                                      [(reservation_id, object_name)], None)
            if isinstance(e, DomainError):
                raise
            logger.error("Failed to create instant upload record (error_type={})", type(e).__name__)
            raise ServiceOperationError("Failed to save file metadata")

        self.files.created(new_file)
        if status != "success":
            self.files.invalidate([cast(int, new_file.id)], workspace_id)
        else:
            self.files.clear_search(workspace_id)
        return new_file


    def preflight_file_upload(self, session: Session, workspace_id: int, uploader_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """上传预检：hash 命中则直接克隆记录，跳过分片传输。"""
        filename = (data.get("filename") or "").strip()
        if not filename:
            raise BusinessRuleError("filename is required")

        total_size = int(data.get("total_size") or 0)
        if total_size <= 0:
            raise BusinessRuleError("total_size must be positive")

        if total_size > self.settings.max_upload_size:
            raise PayloadTooLargeError("File too large")
        content_hash = self.files.normalize_hash(data.get("content_hash"))
        if not content_hash:
            raise BusinessRuleError("content_hash is required")

        source_file = self.reusable_source(session, content_hash, total_size)
        if not source_file:
            return {"instant_upload": False, "exists": False}

        parent_id = self.files.resolve_parent(session, workspace_id, data.get("parent_id"))
        new_file = self.clone_source(session,
            source_file,
            filename=filename,
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            parent_id=parent_id,
            mime_type=data.get("mime_type"),
            content_hash=content_hash,
        )
        return {"instant_upload": True, "exists": True, "file": new_file.to_dict()}
