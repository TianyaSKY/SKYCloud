"""文件夹 CRUD、缓存失效, 以及整理任务的 Redis 分布式锁与入队."""

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
    evict_cache,
    sql_cache_allowed,
    sql_cache_revision,
)
from app.infra.extensions import redis_client
from app.infra.organize_tasks import admit_task
from app.models.file import File, FileDict
from app.models.folder import Folder, FolderDict

from . import repository, tree
from .change_log import FileChangeEventSpec

ORGANIZE_TASK_LOCK_PREFIX = "organize:task:lock"
ORGANIZE_TASK_LOCK_TTL_SECONDS = 6 * 60 * 60

FOLDER_CACHE_PREFIX = "workspace:folders"
ROOT_FOLDER_CACHE_PREFIX = "workspace:root_folder:v2"
ROOT_FILES_CACHE_PREFIX = "workspace:root_files:v2"
FOLDER_CACHE_EXPIRE = 3600


class FolderCreateData(TypedDict):
    """Validated folder creation payload handed to `create_folder`."""

    name: str
    workspace_id: NotRequired[int]
    parent_id: NotRequired[int | None]


class FolderUpdateData(TypedDict):
    """Validated folder update payload handed to `update_folder`."""

    name: NotRequired[str]
    parent_id: NotRequired[int | None]


def _organize_task_lock_key(workspace_id: int) -> str:
    return f"{ORGANIZE_TASK_LOCK_PREFIX}:{workspace_id}"


def _tree_cache_key(session: Session, workspace_id: int) -> str:
    """Cache key suffix bound to the workspace tree revision."""
    revision = sql_cache_revision(session, f"workspace:tree:{workspace_id}")
    return f"{workspace_id}:sql:{revision}"


def _tree_cache_allowed(session: Session, workspace_id: int) -> bool:
    """Use the shared SQL cache gate with the cached tree signature."""
    return sql_cache_allowed(session, workspace_id)


def _invalidate_folder_caches(workspace_id: int) -> None:
    """失效目录相关缓存, 避免列表读到旧结构."""
    evict_cache(FOLDER_CACHE_PREFIX, workspace_id)
    evict_cache(ROOT_FOLDER_CACHE_PREFIX, workspace_id)
    evict_cache(ROOT_FILES_CACHE_PREFIX, workspace_id)


def _require_workspace_id(data: FolderCreateData) -> int:
    """Require the create payload to carry its workspace id."""
    workspace_id = data.get("workspace_id")
    if workspace_id is None:
        message = "workspace_id is required"
        raise PermissionDeniedError(message)
    return workspace_id


def _require_folder(folder: Folder | None) -> Folder:
    """Require a loaded folder row; the caller rolls back on this error."""
    if folder is None:
        message = "Folder not found"
        raise ResourceNotFoundError(message)
    return folder


def _assert_owns(folder: Folder, workspace_id: int) -> None:
    """Reject a folder that belongs to another workspace."""
    if folder.workspace_id != workspace_id:
        message = "Folder belongs to another workspace"
        raise PermissionDeniedError(message)


def create_folder(session: Session, data: FolderCreateData) -> Folder:
    """Create a folder under the resolved parent, locking the tree first."""
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

        if new_folder.workspace_id:
            _invalidate_folder_caches(new_folder.workspace_id)
    except Exception:
        session.rollback()
        raise
    else:
        return new_folder


def get_folder(session: Session, folder_id: int) -> Folder:
    """Load a folder by id or fail with a domain error."""
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
    """Rename or move a folder inside the workspace tree."""
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
        _invalidate_folder_caches(workspace_id)
    except Exception:
        session.rollback()
        raise
    else:
        return folder


def delete_folder(session: Session, folder_id: int) -> None:
    """Delete a folder and its whole subtree, logging one event per entity."""
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
            _invalidate_folder_caches(workspace_id)
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
    """Return the workspace root folder id from the fresh database."""
    return repository.root_id(session, workspace_id)


@cacheable(
    CacheableParams(prefix=ROOT_FILES_CACHE_PREFIX, expire=FOLDER_CACHE_EXPIRE),
    cache_allowed=_tree_cache_allowed,
    key=_tree_cache_key,
)
def get_files_in_root_folder(
    session: Session,
    workspace_id: int,
) -> list[FileDict]:
    """List the file records stored directly in the workspace root folder."""
    root_folder_id = get_root_folder_id(session, workspace_id)
    if not root_folder_id:
        return []

    files = (
        session.query(File)
        .filter_by(workspace_id=workspace_id, parent_id=root_folder_id)
        .all()
    )
    return [f.to_dict() for f in files]


@cacheable(
    CacheableParams(prefix=FOLDER_CACHE_PREFIX, expire=FOLDER_CACHE_EXPIRE),
    cache_allowed=_tree_cache_allowed,
    key=_tree_cache_key,
)
def get_folders(session: Session, workspace_id: int) -> list[FolderDict]:
    """List every folder of the workspace as flat serialised rows."""
    folders = session.query(Folder).filter_by(workspace_id=workspace_id).all()
    return [f.to_dict() for f in folders]


def mark_organize_task_running(workspace_id: int, lock_token: str) -> bool:
    """仅当当前锁 token 匹配时, 将状态改为 running 并续期."""
    script = """
    local current = redis.call('GET', KEYS[1])
    if not current then
        return 0
    end
    if current ~= ARGV[1] .. ':queued' and current ~= ARGV[1] .. ':running' then
        return 0
    end
    redis.call('SET', KEYS[1], ARGV[1] .. ':running', 'EX', ARGV[2])
    return 1
    """
    return bool(
        redis_client.eval(
            script,
            1,
            _organize_task_lock_key(workspace_id),
            lock_token,
            ORGANIZE_TASK_LOCK_TTL_SECONDS,
        ),
    )


def release_organize_task_lock(workspace_id: int, lock_token: str) -> None:
    """仅释放属于当前 token 的锁, 避免误删新任务锁."""
    script = """
    local current = redis.call('GET', KEYS[1])
    if not current then
        return 0
    end
    if current ~= ARGV[1] .. ':queued' and current ~= ARGV[1] .. ':running' then
        return 0
    end
    return redis.call('DEL', KEYS[1])
    """
    redis_client.eval(script, 1, _organize_task_lock_key(workspace_id), lock_token)


def organize_files(session: Session, workspace_id: int, user_id: int) -> bool:
    """Commit one durable admitted task; broker delivery is performed by Worker."""
    return admit_task(session, workspace_id, user_id)
