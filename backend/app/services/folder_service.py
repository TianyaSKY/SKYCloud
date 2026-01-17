import json
from typing import List

from app.extensions import db, redis_client
from app.models.file import File
from app.models.folder import Folder
from app.services.file_service import delete_file, _clear_search_cache


def create_folder(data):
    try:
        new_folder = Folder(
            name=data['name'],
            user_id=data.get('user_id'),
            parent_id=data.get('parent_id')
        )
        db.session.add(new_folder)
        db.session.commit()

        # Invalidate cache
        if new_folder.user_id:
            redis_client.delete(f"user:folders:{new_folder.user_id}")

        return new_folder
    except Exception as e:
        db.session.rollback()
        raise e


def get_folder(id):
    return Folder.query.get_or_404(id)


def update_folder(id, data):
    folder = Folder.query.get_or_404(id)
    try:
        folder.name = data.get('name', folder.name)
        folder.parent_id = data.get('parent_id', folder.parent_id)
        db.session.commit()

        # Invalidate cache
        if folder.user_id:
            redis_client.delete(f"user:folders:{folder.user_id}")

        return folder
    except Exception as e:
        db.session.rollback()

        raise e


def delete_folder(id):
    folder = Folder.query.get_or_404(id)
    user_id = folder.user_id

    try:
        # 递归删除子文件夹和文件
        _delete_folder_recursive(folder)

        db.session.delete(folder)
        db.session.commit()

        # Invalidate cache
        if user_id:
            redis_client.delete(f"user:folders:{user_id}")
            _clear_search_cache(user_id)
    except Exception as e:
        db.session.rollback()
        raise e


def _delete_folder_recursive(folder):
    files = File.query.filter_by(parent_id=folder.id).all()
    for file in files:
        # 递归删除时不单独 commit，由最外层 delete_folder 统一 commit
        delete_file(file.id, commit=False)

    # 递归删除子文件夹
    subfolders = Folder.query.filter_by(parent_id=folder.id).all()
    for subfolder in subfolders:
        _delete_folder_recursive(subfolder)
        db.session.delete(subfolder)


def get_root_folder_id(user_id):
    cache_key = f"user:root:folder:{user_id}"
    cached_id = redis_client.get(cache_key)
    if cached_id:
        return int(cached_id)

    root_folder = Folder.query.filter_by(user_id=user_id, parent_id=None).first()
    if root_folder:
        redis_client.set(cache_key, root_folder.id, ex=3600)
        return root_folder.id
    return None


def get_files_in_root_folder(user_id):
    cache_key = f"user:files:root:{user_id}"
    cached_files = redis_client.get(cache_key)
    if cached_files:
        return [File.from_cache(f) for f in json.loads(cached_files)]

    root_folder_id = get_root_folder_id(user_id)
    if not root_folder_id:
        return []

    files = File.query.filter_by(parent_id=root_folder_id).all()
    return files


def get_folders(user_id) -> List[Folder]:
    cache_key = f"user:folders:{user_id}"
    cached_folders = redis_client.get(cache_key)
    if cached_folders:
        return [Folder.from_cache(f) for f in json.loads(cached_folders)]

    folders = Folder.query.filter_by(user_id=user_id).all()
    folder_list = [folder.to_dict() for folder in folders]
    redis_client.set(cache_key, json.dumps(folder_list), ex=3600)
    return folders


def organize_files(user_id):
    redis_client.rpush("organize_file_queue", user_id)
