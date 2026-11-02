"""分片上传编排层: 结合配额预留、文件发布与字节适配器.

调用方持有 SQL Session。预留行锁在分片写入与
最终发布期间保持持有; 补偿机制完整保留既有事务提交语义。
"""

import math
import re
import uuid
from collections.abc import Callable
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
from app.features.file import upload_authorization, upload_quota
from app.features.file.deletion import clear_search_cache
from app.features.file.publication import (
    hash_file,
    mime_type,
    normalize_hash,
    object_name,
    record_created,
    resolve_parent,
    stage_record,
)
from app.features.file.repository import FileRecordSpec
from app.features.file.upload_config import MultipartSettings, multipart_settings
from app.features.file.upload_quota import CancelParams, ReserveParams
from app.features.file.upload_recovery import compensate_failed_uploads
from app.features.file.upload_service import (
    CloneSourceParams,
    FileUploadService,
    get_upload_service,
)
from app.features.file.upload_types import UploadSource, UploadStorage
from app.infra import multipart_storage
from app.infra.multipart_cleanup import remove_multipart_upload_safely
from app.infra.storage import get_storage_client
from app.infra.upload_staging import StagedUpload, close_staging_safely
from app.models.file import File
from app.models.upload_reservation import UploadReservation


class MultipartInitData(TypedDict, total=False):
    """从请求载荷中解析出的分片上传初始化输入参数."""

    filename: str
    total_size: int
    chunk_size: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str | None
    upload_id: str


class MultipartMetadata(TypedDict):
    """持久化记录在预留表 details 字段中的会话元数据."""

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
    """已完成前置校验、待持久化落库的分片上传会话规格."""

    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int


@dataclass(frozen=True, slots=True, kw_only=True)
class MultipartPublication:
    """各发布步骤共享的所有者标识与已解析元数据."""

    workspace_id: int
    uploader_id: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SaveMultipartChunkParams:
    """持久化存储单个已上传分片所需的输入参数."""

    uploader_id: int
    workspace_id: int
    upload_id: str
    chunk_index: int
    chunk: UploadSource



def _multipart_metadata(reservation: UploadReservation) -> MultipartMetadata:
    """解析并映射本服务为该预留记录写入的 JSON details 元数据."""
    return cast("MultipartMetadata", reservation.details)


def validate_upload_id(upload_id: str) -> str:
    """返回去除首尾空格的 upload_id, 拒绝含有非法字符的值."""
    candidate = (upload_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", candidate):
        message = "Invalid upload_id"
        raise BusinessRuleError(message)
    return candidate


def _require_all_chunks(uploaded_chunks: set[int], total_chunks: int) -> None:
    """若仍有任何分片缺失则拒绝执行合并组装."""
    missing = [index for index in range(total_chunks) if index not in uploaded_chunks]
    if missing:
        preview = ",".join(str(index) for index in missing[:10])
        message = f"Missing chunks: {preview}"
        raise BusinessRuleError(message)


def _verify_content_hash(actual_hash: str, expected_hash: str | None) -> None:
    """若合并后对象的实际摘要与声明的哈希不符则拒绝发布."""
    if expected_hash and actual_hash != expected_hash:
        message = "Uploaded content does not match its hash"
        raise BusinessRuleError(message)


class MultipartUploadService:
    """基于配额预留机制与共享发布阶段实现的分片上传服务."""

    def __init__(
        self,
        settings: MultipartSettings,
        uploads: FileUploadService,
        *,
        storage_factory: Callable[[], UploadStorage] = get_storage_client,
    ) -> None:
        """绑定分片配置、上传用例及对象存储接口."""
        self.settings = settings
        self.uploads = uploads
        self.storage_factory = storage_factory

    def directory(self, workspace_id: int, upload_id: str) -> str:
        """返回指定分片会话的分片暂存目录路径."""
        return multipart_storage.upload_directory(
            self.settings.multipart_root, workspace_id, upload_id,
        )

    def _pending_upload(self, data: MultipartInitData) -> PendingMultipart:
        """校验声明的文件尺寸并推导预期的分片总数."""
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
        """当声明的哈希已存在时, 直接秒传复用该存储对象."""
        content_hash = publication.content_hash
        if not content_hash:
            return None
        source_file = self.uploads.reusable_source(
            session, content_hash, pending.total_size, publication.workspace_id,
        )
        if not source_file:
            return None
        new_file = self.uploads.clone_source(
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
        """创建 (或秒传复用) 用于接收后续分片的上传会话."""
        pending = self._pending_upload(data)
        publication = MultipartPublication(
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            parent_id=resolve_parent(
                session, workspace_id, data.get("parent_id"),
            ),
            mime_type=data.get("mime_type"),
            content_hash=normalize_hash(data.get("content_hash")),
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
                else object_name(pending.filename)
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
                multipart_storage.write_metadata(
                    meta_path, metadata, root=self.settings.multipart_root,
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
        """查询并返回进行中分片会话已记录的分片上传进度."""
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
        """在当前持有的预留锁保护下写入存储单个上传分片."""
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
        """将所有已存储的分片文件合并装配为暂存载荷."""
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
        """发布合并后的存储对象, 随后插入并提交对应文件记录."""
        unique_filename = meta["object_name"]
        total_size = int(meta["total_size"])
        storage: UploadStorage | None = None
        try:
            # 将合并后的文件发布至 MinIO。
            actual_hash = hash_file(staging.path)
            _verify_content_hash(actual_hash, publication.content_hash)
            storage = self.storage_factory()
            storage.upload_file(staging.path, unique_filename)

            resolved_mime = mime_type(staging.path, publication.mime_type)
            record = FileRecordSpec(
                name=meta["filename"],
                file_path=unique_filename,
                file_size=total_size,
                mime_type=resolved_mime,
                workspace_id=publication.workspace_id,
                uploader_id=publication.uploader_id,
                parent_id=publication.parent_id,
                content_hash=actual_hash,
            )
            upload_authorization.consume(
                session,
                publication.workspace_id,
                publication.uploader_id,
                meta["upload_id"],
                total_size,
            )
            new_file = stage_record(session, record)
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
            # 移除合并后的暂存文件及分片存储目录。
            close_staging_safely(staging)
            remove_multipart_upload_safely(
                self.settings.multipart_root,
                publication.workspace_id,
                meta["upload_id"],
            )

        record_created(new_file)
        clear_search_cache(publication.workspace_id)
        return new_file

    def complete_multipart_upload(
        self, session: Session, workspace_id: int, uploader_id: int, upload_id: str,
    ) -> File:
        """组装分片数据, 随后发布合并对象并提交文件记录."""
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
            parent_id=resolve_parent(
                session, workspace_id, meta.get("parent_id"),
            ),
            mime_type=meta.get("mime_type"),
            content_hash=normalize_hash(meta.get("content_hash")),
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
        """取消会话预留记录并删除其分片暂存目录."""
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


def get_multipart_service() -> MultipartUploadService:
    """通过统一配置装配点构建分片上传服务实例."""
    return MultipartUploadService(multipart_settings(), get_upload_service())
