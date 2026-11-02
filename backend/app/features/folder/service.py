"""文件夹 CRUD 与目录树读穿缓存."""

from typing import NotRequired, TypedDict

from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.file.deletion import clear_search_cache, delete_file
from app.features.workspace.permissions import assert_member
from app.infra.cache import (
    CacheableParams,
    cacheable,
    sql_cache_allowed,
    sql_cache_revision,
)
from app.models.file import File
from app.models.folder import Folder, FolderDict

from . import repository, tree
from .change_log import FileChangeEventSpec

FOLDER_CACHE_PREFIX = "workspace:folders"
ROOT_FOLDER_CACHE_PREFIX = "workspace:root_folder:v2"
FOLDER_CACHE_EXPIRE = 3600


class FolderCreateData(TypedDict):
    """传递给 `create_folder` 的已校验文件夹创建入参."""

    name: str
    workspace_id: NotRequired[int]
    parent_id: NotRequired[int | None]


class FolderUpdateData(TypedDict):
    """传递给 `update_folder` 的已校验文件夹更新入参."""

    name: NotRequired[str]
    parent_id: NotRequired[int | None]




def _tree_cache_key(session: Session, workspace_id: int) -> str:
    """绑定至工作空间目录树版本的缓存键后缀."""
    revision = sql_cache_revision(session, f"workspace:tree:{workspace_id}")
    return f"{workspace_id}:sql:{revision}"


def _tree_cache_allowed(session: Session, workspace_id: int) -> bool:
    """基于缓存的目录树签名使用共享 SQL 缓存网关."""
    return sql_cache_allowed(session, workspace_id)


def _require_workspace_id(data: FolderCreateData) -> int:
    """确保创建入参中包含 workspace_id."""
    workspace_id = data.get("workspace_id")
    if workspace_id is None:
        message = "workspace_id is required"
        raise PermissionDeniedError(message)
    return workspace_id


def _require_folder(folder: Folder | None) -> Folder:
    """要求文件夹记录存在; 调用方在此异常时执行事务回滚."""
    if folder is None:
        message = "Folder not found"
        raise ResourceNotFoundError(message)
    return folder


def _assert_owns(folder: Folder, workspace_id: int) -> None:
    """拒绝不属于当前工作空间的文件夹."""
    if folder.workspace_id != workspace_id:
        message = "Folder belongs to another workspace"
        raise PermissionDeniedError(message)


def create_folder(session: Session, data: FolderCreateData) -> Folder:
    """在解析的目标父目录下创建文件夹, 先对目录树加锁."""
    try:
        workspace_id = _require_workspace_id(data)
        repository.lock_tree(session, workspace_id)
        tree.validate_name(data["name"])
        parent_id = tree.validate_parent(session, workspace_id, data.get("parent_id"))
        new_folder = Folder(
            name=data["name"],
            workspace_id=workspace_id,
            parent_id=parent_id,
        )
        session.add(new_folder)
        session.commit()
    except Exception:
        session.rollback()
        raise
    else:
        return new_folder


def get_folder(session: Session, folder_id: int) -> Folder:
    """根据 ID 加载文件夹或抛出领域异常."""
    folder = session.get(Folder, folder_id)
    if not folder:
        message = "Folder not found"
        raise ResourceNotFoundError(message)
    return folder


def get_authorized_folder(
    session: Session,
    workspace_id: int,
    user_id: int,
    folder_id: int,
) -> Folder:
    """校验用户是成员且文件夹属于当前工作空间."""
    assert_member(session, workspace_id, user_id)
    folder = get_folder(session, folder_id)
    if folder.workspace_id != workspace_id:
        message = "Permission denied"
        raise PermissionDeniedError(message)
    return folder


def update_folder(
    session: Session,
    folder_id: int,
    data: FolderUpdateData,
) -> Folder:
    """在工作空间目录树中重命名或移动文件夹."""
    workspace_id = repository.workspace_id_for_folder(session, folder_id)
    if workspace_id is None:
        message = "Folder not found"
        raise ResourceNotFoundError(message)
    try:
        repository.lock_tree(session, workspace_id)
        folder = _require_folder(repository.get_folder(session, folder_id))
        _assert_owns(folder, workspace_id)
        tree.assert_mutable(session, folder)
        if "name" in data:
            tree.validate_name(data["name"])
        if "parent_id" in data:
            parent_id = tree.validate_parent(
                session, workspace_id, data["parent_id"], moving_id=folder_id,
            )
            folder.parent_id = parent_id
        folder.name = data.get("name", folder.name)
        session.commit()
    except Exception:
        session.rollback()
        raise
    else:
        return folder


def delete_folder(session: Session, folder_id: int) -> None:
    """删除文件夹及其全部子树, 并为每个实体记录一条变更事件."""
    workspace_id = repository.workspace_id_for_folder(session, folder_id)
    if workspace_id is None:
        message = "Folder not found"
        raise ResourceNotFoundError(message)
    deleted_events: list[FileChangeEventSpec] = []
    try:
        repository.lock_tree(session, workspace_id)
        folder = _require_folder(repository.lock_folder(session, folder_id))
        _assert_owns(folder, workspace_id)
        tree.assert_mutable(session, folder)
        _delete_folder_recursive(session, folder, deleted_events)
        deleted_events.append(
            {
                "entity_type": "folder",
                "entity_id": folder.id,
                "action": "delete",
                "old_parent_id": folder.parent_id,
                "new_parent_id": None,
                "old_name": folder.name,
                "new_name": None,
            },
        )

        session.delete(folder)
        session.commit()

        if workspace_id:
            clear_search_cache(workspace_id)
    except Exception:
        session.rollback()
        raise


def _delete_folder_recursive(
    session: Session,
    folder: Folder,
    deleted_events: list[FileChangeEventSpec],
    visited: set[int] | None = None,
) -> None:
    visited = set() if visited is None else visited
    if folder.id in visited:
        message = "Invalid folder hierarchy"
        raise BusinessRuleError(message)
    visited.add(folder.id)
    files = (
        session.query(File)
        .filter_by(parent_id=folder.id)
        .order_by(File.id)
        .with_for_update()
        .all()
    )
    for file in files:
        if file.workspace_id != folder.workspace_id:
            message = "Folder contains a file from another workspace"
            raise PermissionDeniedError(message)
        deleted_events.append(
            {
                "entity_type": "file",
                "entity_id": file.id,
                "action": "delete",
                "old_parent_id": file.parent_id,
                "new_parent_id": None,
                "old_name": file.name,
                "new_name": None,
            },
        )
        # 递归删除时不单独 commit, 由最外层 delete_folder 统一 commit
        delete_file(session, file.id, commit=False, log_event=False)

    subfolders = (
        session.query(Folder)
        .filter_by(parent_id=folder.id)
        .order_by(Folder.id)
        .with_for_update()
        .all()
    )
    for subfolder in subfolders:
        if subfolder.workspace_id != folder.workspace_id:
            message = "Folder contains a folder from another workspace"
            raise PermissionDeniedError(message)
        _delete_folder_recursive(session, subfolder, deleted_events, visited)
        deleted_events.append(
            {
                "entity_type": "folder",
                "entity_id": subfolder.id,
                "action": "delete",
                "old_parent_id": subfolder.parent_id,
                "new_parent_id": None,
                "old_name": subfolder.name,
                "new_name": None,
            },
        )
        session.delete(subfolder)


@cacheable(
    CacheableParams(prefix=ROOT_FOLDER_CACHE_PREFIX, expire=FOLDER_CACHE_EXPIRE),
    cache_allowed=_tree_cache_allowed,
    key=_tree_cache_key,
)
def get_root_folder_id(session: Session, workspace_id: int) -> int | None:
    """从最新数据库中查询工作空间根文件夹 ID."""
    return repository.root_id(session, workspace_id)


@cacheable(
    CacheableParams(prefix=FOLDER_CACHE_PREFIX, expire=FOLDER_CACHE_EXPIRE),
    cache_allowed=_tree_cache_allowed,
    key=_tree_cache_key,
)
def get_folders(session: Session, workspace_id: int) -> list[FolderDict]:
    """列出工作空间下所有文件夹的扁平序列化字典列表."""
    folders = session.query(Folder).filter_by(workspace_id=workspace_id).all()
    return [f.to_dict() for f in folders]


