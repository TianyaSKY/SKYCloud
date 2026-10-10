"""文件夹 CRUD、缓存失效，以及整理任务的 Redis 分布式锁与入队。"""

import uuid
from typing import List

from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError, PermissionDeniedError, ResourceNotFoundError
from app.infra.extensions import redis_client
from app.infra.cache import cacheable, evict_cache, sql_cache_allowed, sql_cache_revision
from app.infra.task_queue import publish_organize_task
from app.models.file import File
from app.models.folder import Folder
from . import repository, tree
from app.features.folder import change_log as change_log_service
from app.features.file.service import delete_file, _clear_search_cache
from app.features.workspace.permissions import assert_member

ORGANIZE_TASK_LOCK_PREFIX = "organize:task:lock"
ORGANIZE_TASK_LOCK_TTL_SECONDS = 6 * 60 * 60

FOLDER_CACHE_PREFIX = "workspace:folders"
ROOT_FOLDER_CACHE_PREFIX = "workspace:root_folder:v2"
ROOT_FILES_CACHE_PREFIX = "workspace:root_files:v2"
FOLDER_CACHE_EXPIRE = 3600


def _organize_task_lock_key(workspace_id: int) -> str:
    return f"{ORGANIZE_TASK_LOCK_PREFIX}:{workspace_id}"


def _invalidate_folder_caches(workspace_id: int) -> None:
    """目录树变更后失效相关缓存，避免列表读到旧结构。"""
    evict_cache(FOLDER_CACHE_PREFIX, workspace_id)
    evict_cache(ROOT_FOLDER_CACHE_PREFIX, workspace_id)
    evict_cache(ROOT_FILES_CACHE_PREFIX, workspace_id)


def create_folder(session: Session, data):
    try:
        workspace_id = data.get("workspace_id")
        if workspace_id is None:
            raise PermissionDeniedError("workspace_id is required")
        repository.lock_tree(session, workspace_id)
        tree.validate_name(data['name'])
        parent_id = tree.validate_parent(session, workspace_id, data.get('parent_id'))
        new_folder = Folder(
            name=data["name"],
            workspace_id=workspace_id,
            parent_id=parent_id,
        )
        session.add(new_folder)
        session.commit()

        if new_folder.workspace_id:
            _invalidate_folder_caches(new_folder.workspace_id)

        return new_folder
    except Exception as e:
        session.rollback()
        raise e


def get_folder(session: Session, id):
    folder = session.get(Folder, id)
    if not folder:
        raise ResourceNotFoundError("Folder not found")
    return folder


def get_authorized_folder(session: Session, workspace_id: int, user_id: int, folder_id: int) -> Folder:
    """校验用户是成员且文件夹属于当前工作空间。"""
    assert_member(session, workspace_id, user_id)
    folder = get_folder(session, folder_id)
    if folder.workspace_id != workspace_id:
        raise PermissionDeniedError("Permission denied")
    return folder


def update_folder(session: Session, id, data):
    workspace_id = repository.workspace_id_for_folder(session, id)
    if workspace_id is None:
        raise ResourceNotFoundError("Folder not found")
    try:
        repository.lock_tree(session, workspace_id)
        folder = repository.get_folder(session, id)
        if folder is None:
            raise ResourceNotFoundError("Folder not found")
        if folder.workspace_id != workspace_id:
            raise PermissionDeniedError('Folder belongs to another workspace')
        tree.assert_mutable(session, folder)
        if 'name' in data:
            tree.validate_name(data['name'])
        if 'parent_id' in data:
            parent_id = tree.validate_parent(session, workspace_id, data['parent_id'], moving_id=id)
            folder.parent_id = parent_id
        folder.name = data.get('name', folder.name)
        session.commit()
        _invalidate_folder_caches(workspace_id)
        return folder
    except Exception:
        session.rollback()
        raise


def delete_folder(session: Session, id):
    workspace_id = repository.workspace_id_for_folder(session, id)
    if workspace_id is None:
        raise ResourceNotFoundError("Folder not found")
    deleted_events: list[dict] = []
    try:
        repository.lock_tree(session, workspace_id)
        folder = repository.lock_folder(session, id)
        if folder is None:
            raise ResourceNotFoundError("Folder not found")
        if folder.workspace_id != workspace_id:
            raise PermissionDeniedError('Folder belongs to another workspace')
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
            }
        )

        session.delete(folder)
        session.commit()

        if workspace_id:
            _invalidate_folder_caches(workspace_id)
            _clear_search_cache(workspace_id)
    except Exception as e:
        session.rollback()
        raise e


def _delete_folder_recursive(session: Session, folder, deleted_events: list[dict], visited=None):
    visited = set() if visited is None else visited
    if folder.id in visited:
        raise BusinessRuleError("Invalid folder hierarchy")
    visited.add(folder.id)
    files = session.query(File).filter_by(parent_id=folder.id).order_by(File.id).with_for_update().all()
    for file in files:
        if file.workspace_id != folder.workspace_id:
            raise PermissionDeniedError("Folder contains a file from another workspace")
        deleted_events.append(
            {
                "entity_type": "file",
                "entity_id": file.id,
                "action": "delete",
                "old_parent_id": file.parent_id,
                "new_parent_id": None,
                "old_name": file.name,
                "new_name": None,
            }
        )
        # 递归删除时不单独 commit，由最外层 delete_folder 统一 commit
        delete_file(session, file.id, commit=False, log_event=False)

    subfolders = session.query(Folder).filter_by(parent_id=folder.id).order_by(Folder.id).with_for_update().all()
    for subfolder in subfolders:
        if subfolder.workspace_id != folder.workspace_id:
            raise PermissionDeniedError("Folder contains a folder from another workspace")
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
            }
        )
        session.delete(subfolder)


@cacheable(
    cache_allowed=sql_cache_allowed,
    prefix=ROOT_FOLDER_CACHE_PREFIX,
    expire=FOLDER_CACHE_EXPIRE,
    key=lambda session, workspace_id, **_: f"{workspace_id}:sql:{sql_cache_revision(session, f'workspace:tree:{workspace_id}')}",
)
def get_root_folder_id(session: Session, workspace_id) -> int | None:
    return repository.root_id(session, workspace_id)


@cacheable(
    cache_allowed=sql_cache_allowed,
    prefix=ROOT_FILES_CACHE_PREFIX,
    expire=FOLDER_CACHE_EXPIRE,
    key=lambda session, workspace_id, **_: f"{workspace_id}:sql:{sql_cache_revision(session, f'workspace:tree:{workspace_id}')}",
)
def get_files_in_root_folder(session: Session, workspace_id) -> List[dict]:
    root_folder_id = get_root_folder_id(session, workspace_id)
    if not root_folder_id:
        return []

    files = session.query(File).filter_by(workspace_id=workspace_id, parent_id=root_folder_id).all()
    return [f.to_dict() for f in files]


@cacheable(
    cache_allowed=sql_cache_allowed,
    prefix=FOLDER_CACHE_PREFIX,
    expire=FOLDER_CACHE_EXPIRE,
    key=lambda session, workspace_id, **_: f"{workspace_id}:sql:{sql_cache_revision(session, f'workspace:tree:{workspace_id}')}",
)
def get_folders(session: Session, workspace_id) -> List[dict]:
    folders = session.query(Folder).filter_by(workspace_id=workspace_id).all()
    return [f.to_dict() for f in folders]


def _acquire_organize_task_lock_and_enqueue(workspace_id: int, user_id: int, lock_token: str) -> bool:
    """加锁并入队：仅当锁不存在时设置并发布 RabbitMQ 任务。

    锁值格式: "<token>:<state>"，state 为 queued/running。
    入队失败则释放锁，避免永久占锁。
    """
    lock_key = _organize_task_lock_key(workspace_id)
    acquired = redis_client.set(
        lock_key,
        f"{lock_token}:queued",
        nx=True,
        ex=ORGANIZE_TASK_LOCK_TTL_SECONDS,
    )
    if not acquired:
        return False

    try:
        publish_organize_task(workspace_id, user_id, lock_token)
    except Exception:
        release_organize_task_lock(workspace_id, lock_token)
        raise
    return True


def mark_organize_task_running(workspace_id: int, lock_token: str) -> bool:
    """仅当当前锁 token 匹配时，将状态改为 running 并续期。"""
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
        )
    )


def release_organize_task_lock(workspace_id: int, lock_token: str) -> None:
    """仅释放属于当前 token 的锁，避免误删新任务锁。"""
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
    from app.infra.organize_tasks import admit_task
    return admit_task(session, workspace_id, user_id)
