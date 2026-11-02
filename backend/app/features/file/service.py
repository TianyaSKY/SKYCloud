"""文件浏览、元数据管理、删除与索引相关业务用例.

普通上传与分片会话由各自独立用例实现; 本模块
聚焦于针对已发布文件的操作处理。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

from loguru import logger
from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    DomainError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ServiceOperationError,
)
from app.features.file import repository, upload_quota
from app.features.file.deletion import clear_search_cache, delete_file
from app.features.file.repository import ListDirectoryParams
from app.features.file.tasks import stage_file_tasks
from app.features.file.upload_config import multipart_settings, upload_settings
from app.features.folder import change_log as change_log_service
from app.features.folder import service as folder_service
from app.features.workspace.permissions import (
    assert_can_write_locked,
    assert_member,
)
from app.infra.cache import (
    CacheableParams,
    cacheable,
    sql_cache_allowed,
    sql_cache_revision,
)
from app.infra.extensions import SessionFactory
from app.infra.multipart_cleanup import cleanup_legacy_multipart
from app.infra.owned_work import run_owned
from app.infra.storage import get_storage_client
from app.infra.upload_staging import cleanup_staging
from app.models.file import File

CACHE_EXPIRATION = 3600
ROOT_FILE_CACHE_PREFIX = "workspace:root:file:v2"


class FileUpdateData(TypedDict, total=False):
    """请求中实际提供的文件元数据局部更新字典."""

    name: str
    status: str
    parent_id: int


class BatchDeleteItemData(TypedDict, total=False):
    """批量删除条目: 包含实体 ID 与是否为文件夹标识."""

    id: int
    is_folder: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class GetFilesAndFoldersParams:
    """目录浏览过滤参数、排序规则与分页规格."""

    workspace_id: int
    parent_id: int | None
    page: int = 1
    page_size: int = 10
    name: str | None = None
    sort_by: str = "created_at"
    order: str = "desc"



def get_file(session: Session, file_id: int) -> File:
    """查询单条文件记录, 若记录不存在则抛出异常."""
    file_obj = repository.find_file(session, file_id)
    if not file_obj:
        message = "File not found"
        raise ResourceNotFoundError(message)
    return file_obj


def get_authorized_file(
    session: Session, workspace_id: int, user_id: int, file_id: int,
) -> File:
    """校验用户的工作空间成员资格以及文件归属该工作空间.

    ``user_id`` 仅用于成员资格校验, 绝不可作为工作空间 ID,
    也不作为上传者所有权检查。
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
    """返回已通过鉴权且底层存储对象确实存在的文件实体."""
    file_obj = get_authorized_file(session, workspace_id, user_id, file_id)
    storage = get_storage_client()
    if not storage.file_exists(file_obj.file_path):
        message = "File not found on server"
        raise ResourceNotFoundError(message)
    return file_obj


def get_files_and_folders(
    session: Session, params: GetFilesAndFoldersParams,
) -> dict[str, object]:
    """返回包含子文件夹与单页文件列表的目录分页数据."""
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
    """应用元数据局部更新并记录对应的文件变更事件流水."""
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
    """基于当前工作空间目录树版本计算根目录文件缓存键."""
    revision = sql_cache_revision(session, f"workspace:tree:{workspace_id}")
    return f"{workspace_id}:sql:{revision}"


def _root_file_cache_allowed(session: Session, workspace_id: int) -> bool:
    """基于缓存的根目录文件签名使用共享 SQL 缓存网关."""
    return sql_cache_allowed(session, workspace_id)


@cacheable(
    CacheableParams(prefix=ROOT_FILE_CACHE_PREFIX, expire=CACHE_EXPIRATION),
    cache_allowed=_root_file_cache_allowed,
    key=_root_file_cache_key,
)
def get_root_file_id(session: Session, workspace_id: int) -> int | None:
    """返回工作空间根目录下的首个文件 ID."""
    root_file = repository.find_root_file(session, workspace_id)
    return root_file.id if root_file else None


def batch_delete_items(
    session: Session,
    workspace_id: int,
    user_id: int,
    items: Sequence[BatchDeleteItemData],
) -> None:
    """在完成写权限检查后批量删除列表中声明的所有文件或目录."""
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
    """将单个索引失败的文件重新推入索引任务队列."""
    file_obj = repository.find_file(session, file_id)
    if not file_obj:
        return
    file_obj.status = "pending"
    stage_file_tasks(session, [file_obj.id])
    session.commit()


def rebuild_failed_indexes(session: Session, workspace_id: int | None = None) -> int:
    """将工作空间内全部索引失败的文件重新推入队列并在最后统一提交."""
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
    """返回工作空间下的全部文件记录行."""
    return repository.list_workspace_files(session, workspace_id)


async def process_status(
    session: Session | None,
    workspace_id: int,
    *,
    session_factory: SessionFactory | None = None,
    user_id: int | None = None,
) -> dict[str, int]:
    """在持有 SQL Session 的线程上聚合文件索引状态统计."""

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
    """按状态对工作空间文件进行分组聚合统计, 无需加载完整实体行."""
    cnt = repository.status_counts(session, workspace_id)
    return {
        "处理中": cnt.get("pending", 0) + cnt.get("processing", 0),
        "成功": cnt.get("success", 0),
        "失败": cnt.get("fail", 0),
    }


def cleanup_expired_uploads(
    max_age_hours: int = 24, *, session: Session | None = None,
) -> int:
    """清理过期的暂存/分片上传目录并关闭僵尸会话记录."""
    upload_config = upload_settings()
    multipart_config = multipart_settings()
    count = cleanup_staging(upload_config.upload_root, upload_quota.TTL_SECONDS)
    count += (
        upload_quota.cleanup(
            session,
            multipart_config.multipart_root,
            upload_root=upload_config.upload_root,
            storage_factory=get_storage_client,
        )
        if session is not None
        else 0
    )
    active_ids: set[tuple[str, str]] = (
        upload_quota.active_reservation_ids(session) if session is not None else set()
    )
    return count + cleanup_legacy_multipart(
        multipart_config.multipart_root, max_age_hours * 3600, active_ids,
    )
