"""文件夹 CRUD、缓存失效，以及整理任务的 Redis 分布式锁与入队。"""

import uuid
from typing import List

from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError, ResourceNotFoundError
from app.extensions import redis_client
from app.infra.cache import cacheable, evict_cache
from app.infra.task_queue import publish_organize_task
from app.models.file import File
from app.models.folder import Folder
from app.services import change_log_service
from app.services.file_service import delete_file, _clear_search_cache

ORGANIZE_TASK_LOCK_PREFIX = "organize:task:lock"
ORGANIZE_TASK_LOCK_TTL_SECONDS = 6 * 60 * 60

FOLDER_CACHE_PREFIX = "user:folders"
ROOT_FOLDER_CACHE_PREFIX = "user:root_folder"
ROOT_FILES_CACHE_PREFIX = "user:root_files"
FOLDER_CACHE_EXPIRE = 3600


def _organize_task_lock_key(user_id: int) -> str:
    return f"{ORGANIZE_TASK_LOCK_PREFIX}:{user_id}"


def _invalidate_folder_caches(user_id: int) -> None:
    """目录树变更后失效相关缓存，避免列表读到旧结构。"""
    evict_cache(FOLDER_CACHE_PREFIX, user_id)
    evict_cache(ROOT_FOLDER_CACHE_PREFIX, user_id)
    evict_cache(ROOT_FILES_CACHE_PREFIX, user_id)


def create_folder(session: Session, data):
    try:
        new_folder = Folder(
            name=data["name"],
            user_id=data.get("user_id"),
            parent_id=data.get("parent_id"),
        )
        session.add(new_folder)
        session.commit()

        if new_folder.user_id:
            change_log_service.log_event(
                user_id=new_folder.user_id,
                entity_type="folder",
                entity_id=new_folder.id,
                action="create",
                old_parent_id=None,
                new_parent_id=new_folder.parent_id,
                old_name=None,
                new_name=new_folder.name,
            )

        if new_folder.user_id:
            _invalidate_folder_caches(new_folder.user_id)

        return new_folder
    except Exception as e:
        session.rollback()
        raise e


def get_folder(session: Session, id):
    folder = session.get(Folder, id)
    if not folder:
        raise ResourceNotFoundError("Folder not found")
    return folder


def get_authorized_folder(session: Session, user_id: int, role: str, folder_id: int) -> Folder:
    """非 admin 仅可访问自己的文件夹。"""
    folder = get_folder(session, folder_id)
    if role != "admin" and folder.user_id != user_id:
        raise PermissionDeniedError("Permission denied")
    return folder


def update_folder(session: Session, id, data):
    folder = session.get(Folder, id)
    if not folder:
        raise ResourceNotFoundError("Folder not found")
    try:
        old_name = folder.name
        old_parent_id = folder.parent_id

        folder.name = data.get("name", folder.name)
        folder.parent_id = data.get("parent_id", folder.parent_id)
        session.commit()

        name_changed = folder.name != old_name
        parent_changed = folder.parent_id != old_parent_id
        if name_changed or parent_changed:
            action = "update_meta"
            if name_changed and not parent_changed:
                action = "rename"
            elif parent_changed and not name_changed:
                action = "move"

            change_log_service.log_event(
                user_id=folder.user_id,
                entity_type="folder",
                entity_id=folder.id,
                action=action,
                old_parent_id=old_parent_id,
                new_parent_id=folder.parent_id,
                old_name=old_name,
                new_name=folder.name,
            )

        if folder.user_id:
            _invalidate_folder_caches(folder.user_id)

        return folder
    except Exception as e:
        session.rollback()

        raise e


def delete_folder(session: Session, id):
    folder = session.get(Folder, id)
    if not folder:
        raise ResourceNotFoundError("Folder not found")
    user_id = folder.user_id
    deleted_events: list[dict] = []

    try:
        # 先递归收集子树删除事件，最后统一 commit / 写变更日志
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

        if user_id and deleted_events:
            change_log_service.log_events_batch(user_id, deleted_events)

        if user_id:
            _invalidate_folder_caches(user_id)
            _clear_search_cache(user_id)
    except Exception as e:
        session.rollback()
        raise e


def _delete_folder_recursive(session: Session, folder, deleted_events: list[dict]):
    files = session.query(File).filter_by(parent_id=folder.id).all()
    for file in files:
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

    subfolders = session.query(Folder).filter_by(parent_id=folder.id).all()
    for subfolder in subfolders:
        _delete_folder_recursive(session, subfolder, deleted_events)
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
    prefix=ROOT_FOLDER_CACHE_PREFIX,
    expire=FOLDER_CACHE_EXPIRE,
    key=lambda session, user_id, **_: user_id,
)
def get_root_folder_id(session: Session, user_id) -> int | None:
    root_folder = session.query(Folder).filter_by(user_id=user_id, parent_id=None).first()
    if root_folder:
        return root_folder.id
    return None


@cacheable(
    prefix=ROOT_FILES_CACHE_PREFIX,
    expire=FOLDER_CACHE_EXPIRE,
    key=lambda session, user_id, **_: user_id,
)
def get_files_in_root_folder(session: Session, user_id) -> List[dict]:
    root_folder_id = get_root_folder_id(session, user_id)
    if not root_folder_id:
        return []

    files = session.query(File).filter_by(parent_id=root_folder_id).all()
    return [f.to_dict() for f in files]


@cacheable(
    prefix=FOLDER_CACHE_PREFIX,
    expire=FOLDER_CACHE_EXPIRE,
    key=lambda session, user_id, **_: user_id,
)
def get_folders(session: Session, user_id) -> List[dict]:
    folders = session.query(Folder).filter_by(user_id=user_id).all()
    return [f.to_dict() for f in folders]


def _acquire_organize_task_lock_and_enqueue(user_id: int, lock_token: str) -> bool:
    """加锁并入队：仅当锁不存在时设置并发布 RabbitMQ 任务。

    锁值格式: "<token>:<state>"，state 为 queued/running。
    入队失败则释放锁，避免永久占锁。
    """
    lock_key = _organize_task_lock_key(user_id)
    acquired = redis_client.set(
        lock_key,
        f"{lock_token}:queued",
        nx=True,
        ex=ORGANIZE_TASK_LOCK_TTL_SECONDS,
    )
    if not acquired:
        return False

    try:
        publish_organize_task(user_id, lock_token)
    except Exception:
        release_organize_task_lock(user_id, lock_token)
        raise
    return True


def mark_organize_task_running(user_id: int, lock_token: str) -> None:
    """仅当当前锁 token 匹配时，将状态改为 running 并续期。"""
    script = """
    local current = redis.call('GET', KEYS[1])
    if not current then
        return 0
    end
    if string.sub(current, 1, string.len(ARGV[1])) ~= ARGV[1] then
        return 0
    end
    redis.call('SET', KEYS[1], ARGV[1] .. ':running', 'EX', ARGV[2])
    return 1
    """
    redis_client.eval(
        script,
        1,
        _organize_task_lock_key(user_id),
        lock_token,
        ORGANIZE_TASK_LOCK_TTL_SECONDS,
    )


def release_organize_task_lock(user_id: int, lock_token: str) -> None:
    """仅释放属于当前 token 的锁，避免误删新任务锁。"""
    script = """
    local current = redis.call('GET', KEYS[1])
    if not current then
        return 0
    end
    if string.sub(current, 1, string.len(ARGV[1])) ~= ARGV[1] then
        return 0
    end
    return redis.call('DEL', KEYS[1])
    """
    redis_client.eval(script, 1, _organize_task_lock_key(user_id), lock_token)


def organize_files(user_id: int) -> bool:
    """入队整理任务；同用户已有任务在排队/执行时返回 False。"""
    lock_token = str(uuid.uuid4())
    return _acquire_organize_task_lock_and_enqueue(user_id, lock_token)
