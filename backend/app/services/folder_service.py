import json
from typing import List

from fastapi import HTTPException
from fastapi_cache.decorator import cache

from app.extensions import db, redis_client
from app.models.file import File
from app.models.folder import Folder
from app.services.file_service import delete_file, _clear_search_cache


def create_folder(data):
    try:
        new_folder = Folder(
            name=data["name"],
            user_id=data.get("user_id"),
            parent_id=data.get("parent_id"),
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
    folder = Folder.query.get(id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


def update_folder(id, data):
    folder = Folder.query.get(id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    try:
        folder.name = data.get("name", folder.name)
        folder.parent_id = data.get("parent_id", folder.parent_id)
        db.session.commit()

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


def _root_folder_key_builder(func, namespace, request, response, *args, **kwargs):
    user_id = kwargs.get("user_id") or args[0]
    return f"user:root:folder:{user_id}"


@cache(expire=3600, key_builder=_root_folder_key_builder)
async def get_root_folder_id(user_id) -> int | None:
    root_folder = Folder.query.filter_by(user_id=user_id, parent_id=None).first()
    if root_folder:
        return root_folder.id
    return None


def _files_root_key_builder(func, namespace, request, response, *args, **kwargs):
    user_id = kwargs.get("user_id") or args[0]
    return f"user:files:root:{user_id}"


@cache(expire=3600, key_builder=_files_root_key_builder)
async def get_files_in_root_folder(user_id) -> List[dict]:
    root_folder_id = await get_root_folder_id(user_id)
    if not root_folder_id:
        return []

    files = File.query.filter_by(parent_id=root_folder_id).all()
    return [f.to_dict() for f in files]


def _folders_key_builder(func, namespace, request, response, *args, **kwargs):
    user_id = kwargs.get("user_id") or args[0]
    return f"user:folders:{user_id}"


@cache(expire=3600, key_builder=_folders_key_builder)
async def get_folders(user_id) -> List[dict]:
    folders = Folder.query.filter_by(user_id=user_id).all()
    return [f.to_dict() for f in folders]


def organize_files(user_id):
    redis_client.rpush("organize_file_queue", user_id)
