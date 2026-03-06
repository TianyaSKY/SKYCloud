import json
import uuid
from typing import List

from fastapi import HTTPException
from fastapi_cache.decorator import cache

from app.extensions import db, redis_client
from app.models.file import File
from app.models.folder import Folder
from app.services import change_log_service
from app.services.file_service import ORGANIZE_FILE_QUEUE, delete_file, _clear_search_cache

ORGANIZE_TASK_LOCK_PREFIX = "organize:task:lock"
ORGANIZE_TASK_LOCK_TTL_SECONDS = 6 * 60 * 60


def _organize_task_lock_key(user_id: int) -> str:
    return f"{ORGANIZE_TASK_LOCK_PREFIX}:{user_id}"


def create_folder(data):
    try:
        new_folder = Folder(
            name=data["name"],
            user_id=data.get("user_id"),
            parent_id=data.get("parent_id"),
        )
        db.session.add(new_folder)
        db.session.commit()

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

        # Invalidate cache
        if new_folder.user_id:
            redis_client.delete(f"user:folders:{new_folder.user_id}")

        return new_folder
    except Exception as e:
        db.session.rollback()
        raise e


def get_folder(id):
    folder = Folder.query.get(id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


def update_folder(id, data):
    folder = Folder.query.get(id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    try:
        old_name = folder.name
        old_parent_id = folder.parent_id

        folder.name = data.get("name", folder.name)
        folder.parent_id = data.get("parent_id", folder.parent_id)
        db.session.commit()

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

        # Invalidate cache
        if folder.user_id:
            redis_client.delete(f"user:folders:{folder.user_id}")

        return folder
    except Exception as e:
        db.session.rollback()

        raise e


def delete_folder(id):
    folder = Folder.query.get(id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    user_id = folder.user_id
    deleted_events: list[dict] = []

    try:
        # 递归删除子文件夹和文件
        _delete_folder_recursive(folder, deleted_events)
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

        db.session.delete(folder)
        db.session.commit()

        if user_id and deleted_events:
            change_log_service.log_events_batch(user_id, deleted_events)

        # Invalidate cache
        if user_id:
            redis_client.delete(f"user:folders:{user_id}")
            _clear_search_cache(user_id)
    except Exception as e:
        db.session.rollback()
        raise e


def _delete_folder_recursive(folder, deleted_events: list[dict]):
    files = File.query.filter_by(parent_id=folder.id).all()
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
        delete_file(file.id, commit=False, log_event=False)

    # 递归删除子文件夹
    subfolders = Folder.query.filter_by(parent_id=folder.id).all()
    for subfolder in subfolders:
        _delete_folder_recursive(subfolder, deleted_events)
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
        db.session.delete(subfolder)


@cache(expire=3600)
async def get_root_folder_id(user_id) -> int | None:
    root_folder = Folder.query.filter_by(user_id=user_id, parent_id=None).first()
    if root_folder:
        return root_folder.id
    return None


@cache(expire=3600)
async def get_files_in_root_folder(user_id) -> List[dict]:
    root_folder_id = await get_root_folder_id(user_id)
    if not root_folder_id:
        return []

    files = File.query.filter_by(parent_id=root_folder_id).all()
    return [f.to_dict() for f in files]


@cache(expire=3600)
async def get_folders(user_id) -> List[dict]:
    folders = Folder.query.filter_by(user_id=user_id).all()
    return [f.to_dict() for f in folders]


def _acquire_organize_task_lock_and_enqueue(user_id: int, lock_token: str) -> bool:
    """
    原子加锁并入队：仅当锁不存在时设置锁并入队。
    锁值格式: "<token>:<state>"，state 为 queued/running。
    """
    lock_key = _organize_task_lock_key(user_id)
    queue_payload = json.dumps({"user_id": user_id, "lock_token": lock_token})
    script = """
    if redis.call('EXISTS', KEYS[1]) == 1 then
        return 0
    end
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    redis.call('RPUSH', KEYS[2], ARGV[3])
    return 1
    """
    result = redis_client.eval(
        script,
        2,
        lock_key,
        ORGANIZE_FILE_QUEUE,
        f"{lock_token}:queued",
        ORGANIZE_TASK_LOCK_TTL_SECONDS,
        queue_payload,
    )
    return bool(result)


def mark_organize_task_running(user_id: int, lock_token: str) -> None:
    """
    仅当当前锁 token 匹配时，将状态改为 running 并续期。
    """
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
    """
    仅释放属于当前 token 的锁，避免误删新任务锁。
    """
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
    """入队整理任务；若已有同用户任务在排队/执行，则返回 False。"""
    lock_token = str(uuid.uuid4())
    return _acquire_organize_task_lock_and_enqueue(user_id, lock_token)
