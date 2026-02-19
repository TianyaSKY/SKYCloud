import json
import logging
import math
import mimetypes
import os
import re
import shutil
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Union

from fastapi import HTTPException
from fastapi_cache.decorator import cache
from openai import OpenAI
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.extensions import UPLOAD_FOLDER, db, redis_client
from app.models.file import File
from app.models.folder import Folder
from app.services.model_config import get_embedding_model_config

logger = logging.getLogger(__name__)

FILE_PROCESS_QUEUE = "file_process_queue"
ORGANIZE_FILE_QUEUE = "organize_file_queue"
CACHE_EXPIRATION = 3600

COPY_BUFFER_SIZE = 1024 * 1024
DEFAULT_CHUNK_SIZE = 2 * 1024 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024
MAX_UPLOAD_SIZE = 0
MAX_TOTAL_CHUNKS = 10000
UPLOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
MULTIPART_ROOT = os.path.join(UPLOAD_FOLDER, ".multipart")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


DEFAULT_CHUNK_SIZE = _env_int("UPLOAD_CHUNK_SIZE", DEFAULT_CHUNK_SIZE)
MAX_CHUNK_SIZE = _env_int("UPLOAD_MAX_CHUNK_SIZE", MAX_CHUNK_SIZE)
MAX_UPLOAD_SIZE = _env_int("UPLOAD_MAX_FILE_SIZE", MAX_UPLOAD_SIZE)
MAX_TOTAL_CHUNKS = _env_int("UPLOAD_MAX_TOTAL_CHUNKS", MAX_TOTAL_CHUNKS)


def _generate_unique_filename(original_filename: str) -> str:
    base_name, extension = os.path.splitext(original_filename)
    safe_base_name = secure_filename(base_name)
    if safe_base_name:
        return f"{uuid.uuid4().hex}_{safe_base_name}{extension}"
    return f"{uuid.uuid4().hex}{extension}"


def _resolve_mime_type(path: str, provided: Optional[str] = None) -> Optional[str]:
    if provided:
        return provided
    return mimetypes.guess_type(path)[0]


def _push_processing_queue(file_ids: List[int], uploader_id: Optional[int]) -> None:
    if not file_ids:
        return
    try:
        pipe = redis_client.pipeline()
        for file_id in file_ids:
            pipe.rpush(FILE_PROCESS_QUEUE, file_id)
        pipe.execute()
        if uploader_id:
            _clear_search_cache(uploader_id)
    except Exception as e:
        logger.exception(f"Error pushing to Redis queue: {e}")


def _multipart_upload_dir(uploader_id: int, upload_id: str) -> str:
    return os.path.join(MULTIPART_ROOT, str(uploader_id), upload_id)


def _multipart_chunks_dir(upload_dir: str) -> str:
    return os.path.join(upload_dir, "chunks")


def _multipart_meta_path(upload_dir: str) -> str:
    return os.path.join(upload_dir, "meta.json")


def _safe_upload_id(upload_id: str) -> str:
    candidate = (upload_id or "").strip()
    if not candidate or not UPLOAD_ID_PATTERN.fullmatch(candidate):
        raise HTTPException(status_code=400, detail="Invalid upload_id")
    return candidate


def _list_uploaded_chunks(chunks_dir: str) -> List[int]:
    uploaded = []
    if not os.path.isdir(chunks_dir):
        return uploaded
    for name in os.listdir(chunks_dir):
        if not name.endswith(".part"):
            continue
        idx = name[:-5]
        if idx.isdigit():
            uploaded.append(int(idx))
    uploaded.sort()
    return uploaded


def _load_multipart_meta(uploader_id: int, upload_id: str) -> Dict[str, Any]:
    safe_upload_id = _safe_upload_id(upload_id)
    upload_dir = _multipart_upload_dir(uploader_id, safe_upload_id)
    meta_path = _multipart_meta_path(upload_dir)
    if not os.path.exists(meta_path):
        raise HTTPException(status_code=404, detail="Upload session not found")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as e:
        logger.exception(f"Failed to read upload metadata: {e}")
        raise HTTPException(status_code=500, detail="Upload session corrupted")

    if int(meta.get("uploader_id", -1)) != uploader_id:
        raise HTTPException(status_code=403, detail="Permission denied")
    return meta


def _write_multipart_meta(meta_path: str, meta: Dict[str, Any]) -> None:
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def create_file(file_obj: Union[FileStorage, Any], data: Dict[str, Any]) -> File:
    upload_folder = UPLOAD_FOLDER
    os.makedirs(upload_folder, exist_ok=True)

    original_filename = file_obj.filename or "unknown"
    unique_filename = _generate_unique_filename(original_filename)
    full_path = os.path.join(upload_folder, unique_filename)

    file_obj.save(full_path)
    file_size = os.path.getsize(full_path)
    mime_type = _resolve_mime_type(full_path, getattr(file_obj, "mimetype", None))

    new_file = File(
        name=original_filename,
        file_path=unique_filename,
        file_size=file_size,
        mime_type=mime_type,
        uploader_id=data.get("uploader_id"),
        parent_id=data.get("parent_id"),
    )
    db.session.add(new_file)
    db.session.commit()

    _push_processing_queue([new_file.id], new_file.uploader_id)
    return new_file


def batch_create_files(
    file_objs: List[Union[FileStorage, Any]], data: Dict[str, Any]
) -> List[File]:
    upload_folder = UPLOAD_FOLDER
    os.makedirs(upload_folder, exist_ok=True)

    new_files = []
    uploader_id = data.get("uploader_id")

    for file_obj in file_objs:
        if not file_obj or not file_obj.filename:
            continue

        original_filename = file_obj.filename
        unique_filename = _generate_unique_filename(original_filename)
        full_path = os.path.join(upload_folder, unique_filename)

        file_obj.save(full_path)
        file_size = os.path.getsize(full_path)
        mime_type = _resolve_mime_type(full_path, getattr(file_obj, "mimetype", None))

        new_file = File(
            name=original_filename,
            file_path=unique_filename,
            file_size=file_size,
            mime_type=mime_type,
            uploader_id=uploader_id,
            parent_id=data.get("parent_id"),
        )
        new_files.append(new_file)

    if not new_files:
        return []

    db.session.add_all(new_files)
    db.session.commit()

    _push_processing_queue([f.id for f in new_files], uploader_id)
    return new_files


def init_multipart_upload(uploader_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    filename = (data.get("filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")

    total_size = int(data.get("total_size") or 0)
    if total_size <= 0:
        raise HTTPException(status_code=400, detail="total_size must be positive")
    if 0 < MAX_UPLOAD_SIZE < total_size:
        raise HTTPException(status_code=413, detail="File too large")

    chunk_size = int(data.get("chunk_size") or DEFAULT_CHUNK_SIZE)
    if chunk_size <= 0:
        raise HTTPException(status_code=400, detail="chunk_size must be positive")
    if chunk_size > MAX_CHUNK_SIZE:
        raise HTTPException(status_code=400, detail="chunk_size exceeds server limit")

    total_chunks = math.ceil(total_size / chunk_size)
    if total_chunks <= 0 or total_chunks > MAX_TOTAL_CHUNKS:
        raise HTTPException(status_code=400, detail="total_chunks exceeds server limit")

    parent_id = data.get("parent_id")
    mime_type = data.get("mime_type")

    upload_id = data.get("upload_id") or uuid.uuid4().hex
    upload_id = _safe_upload_id(upload_id)

    upload_dir = _multipart_upload_dir(uploader_id, upload_id)
    chunks_dir = _multipart_chunks_dir(upload_dir)
    meta_path = _multipart_meta_path(upload_dir)

    os.makedirs(chunks_dir, exist_ok=True)

    if os.path.exists(meta_path):
        meta = _load_multipart_meta(uploader_id, upload_id)
        expected = (
            meta.get("filename"),
            int(meta.get("total_size", 0)),
            int(meta.get("chunk_size", 0)),
            meta.get("parent_id"),
        )
        current = (filename, total_size, chunk_size, parent_id)
        if expected != current:
            raise HTTPException(
                status_code=409,
                detail="upload_id already exists with different file metadata",
            )
    else:
        meta = {
            "upload_id": upload_id,
            "uploader_id": uploader_id,
            "filename": filename,
            "total_size": total_size,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "parent_id": parent_id,
            "mime_type": mime_type,
        }
        _write_multipart_meta(meta_path, meta)

    return {
        "upload_id": upload_id,
        "chunk_size": int(meta["chunk_size"]),
        "total_chunks": int(meta["total_chunks"]),
        "uploaded_chunks": _list_uploaded_chunks(chunks_dir),
    }


def get_multipart_upload_status(uploader_id: int, upload_id: str) -> Dict[str, Any]:
    meta = _load_multipart_meta(uploader_id, upload_id)
    upload_dir = _multipart_upload_dir(uploader_id, _safe_upload_id(upload_id))
    chunks_dir = _multipart_chunks_dir(upload_dir)
    return {
        "upload_id": meta["upload_id"],
        "chunk_size": int(meta["chunk_size"]),
        "total_chunks": int(meta["total_chunks"]),
        "uploaded_chunks": _list_uploaded_chunks(chunks_dir),
    }


def save_multipart_chunk(
    uploader_id: int,
    upload_id: str,
    chunk_index: int,
    chunk_obj: Union[FileStorage, Any],
) -> Dict[str, Any]:
    safe_upload_id = _safe_upload_id(upload_id)
    meta = _load_multipart_meta(uploader_id, safe_upload_id)

    try:
        idx = int(chunk_index)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid chunk_index")

    total_chunks = int(meta["total_chunks"])
    chunk_size = int(meta["chunk_size"])
    total_size = int(meta["total_size"])

    if idx < 0 or idx >= total_chunks:
        raise HTTPException(status_code=400, detail="chunk_index out of range")

    upload_dir = _multipart_upload_dir(uploader_id, safe_upload_id)
    chunks_dir = _multipart_chunks_dir(upload_dir)
    os.makedirs(chunks_dir, exist_ok=True)

    part_path = os.path.join(chunks_dir, f"{idx}.part")
    tmp_part_path = f"{part_path}.tmp"

    if os.path.exists(tmp_part_path):
        os.remove(tmp_part_path)

    chunk_obj.save(tmp_part_path)
    actual_size = os.path.getsize(tmp_part_path)

    expected_size = chunk_size
    if idx == total_chunks - 1:
        expected_size = total_size - (idx * chunk_size)
        if expected_size <= 0:
            expected_size = chunk_size

    if actual_size != expected_size:
        os.remove(tmp_part_path)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid chunk size for index {idx}: expected {expected_size}, got {actual_size}",
        )

    os.replace(tmp_part_path, part_path)

    return {
        "upload_id": safe_upload_id,
        "chunk_index": idx,
        "uploaded_chunks": _list_uploaded_chunks(chunks_dir),
    }


def complete_multipart_upload(uploader_id: int, upload_id: str) -> File:
    safe_upload_id = _safe_upload_id(upload_id)
    meta = _load_multipart_meta(uploader_id, safe_upload_id)

    total_chunks = int(meta["total_chunks"])
    total_size = int(meta["total_size"])
    filename = meta["filename"]
    parent_id = meta.get("parent_id")
    mime_type = meta.get("mime_type")

    upload_dir = _multipart_upload_dir(uploader_id, safe_upload_id)
    chunks_dir = _multipart_chunks_dir(upload_dir)

    uploaded_chunks = set(_list_uploaded_chunks(chunks_dir))
    missing = [idx for idx in range(total_chunks) if idx not in uploaded_chunks]
    if missing:
        preview = ",".join(str(i) for i in missing[:10])
        raise HTTPException(status_code=400, detail=f"Missing chunks: {preview}")

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    unique_filename = _generate_unique_filename(filename)
    final_path = os.path.join(UPLOAD_FOLDER, unique_filename)
    tmp_final_path = f"{final_path}.assembling"

    if os.path.exists(tmp_final_path):
        os.remove(tmp_final_path)

    try:
        with open(tmp_final_path, "wb") as target:
            for idx in range(total_chunks):
                part_path = os.path.join(chunks_dir, f"{idx}.part")
                with open(part_path, "rb") as source:
                    shutil.copyfileobj(source, target, length=COPY_BUFFER_SIZE)

        assembled_size = os.path.getsize(tmp_final_path)
        if assembled_size != total_size:
            os.remove(tmp_final_path)
            raise HTTPException(
                status_code=400,
                detail=f"Assembled file size mismatch: expected {total_size}, got {assembled_size}",
            )
        os.replace(tmp_final_path, final_path)
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(tmp_final_path):
            os.remove(tmp_final_path)
        logger.exception(f"Failed to merge chunks: {e}")
        raise HTTPException(status_code=500, detail="Failed to merge chunks")

    try:
        resolved_mime = _resolve_mime_type(final_path, mime_type)
        new_file = File(
            name=filename,
            file_path=unique_filename,
            file_size=total_size,
            mime_type=resolved_mime,
            uploader_id=uploader_id,
            parent_id=parent_id,
        )
        db.session.add(new_file)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Failed to persist merged file: {e}")
        if os.path.exists(final_path):
            os.remove(final_path)
        raise HTTPException(status_code=500, detail="Failed to save file metadata")
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)

    _push_processing_queue([new_file.id], uploader_id)
    return new_file


def abort_multipart_upload(uploader_id: int, upload_id: str) -> None:
    safe_upload_id = _safe_upload_id(upload_id)
    upload_dir = _multipart_upload_dir(uploader_id, safe_upload_id)
    shutil.rmtree(upload_dir, ignore_errors=True)


def get_file(id: int) -> File:
    file_obj = File.query.get(id)
    if not file_obj:
        raise HTTPException(status_code=404, detail="File not found")
    return file_obj


def get_files_and_folders(
    user_id: int,
    parent_id: Optional[int],
    page: int = 1,
    page_size: int = 10,
    name: Optional[str] = None,
    sort_by: str = "created_at",
    order: str = "desc",
) -> Dict[str, Any]:
    if page < 1:
        page = 1

    folder_query = Folder.query.filter_by(user_id=user_id, parent_id=parent_id)
    if name:
        folder_query = folder_query.filter(Folder.name.ilike(f"%{name}%"))
    if order == "asc":
        folder_query = folder_query.order_by(Folder.name.asc())
    else:
        folder_query = folder_query.order_by(Folder.name.desc())

    file_query = File.query.filter_by(uploader_id=user_id, parent_id=parent_id)
    if name:
        file_query = file_query.filter(File.name.ilike(f"%{name}%"))

    if sort_by == "name":
        sort_column = File.name
    elif sort_by == "size":
        sort_column = File.file_size
    else:
        sort_column = File.created_at

    if order == "asc":
        file_query = file_query.order_by(sort_column.asc())
    else:
        file_query = file_query.order_by(sort_column.desc())

    total_folders = folder_query.count()
    total_files = file_query.count()
    total_items = total_folders + total_files
    total_pages = (total_items + page_size - 1) // page_size if page_size > 0 else 1
    offset = (page - 1) * page_size

    folders_result = []
    files_result = []

    if offset < total_folders:
        folders_result = folder_query.offset(offset).limit(page_size).all()
        fetched_folders_count = len(folders_result)
        if fetched_folders_count < page_size:
            remaining_slots = page_size - fetched_folders_count
            files_result = file_query.offset(0).limit(remaining_slots).all()
    else:
        file_offset = offset - total_folders
        files_result = file_query.offset(file_offset).limit(page_size).all()

    return {
        "folders": [folder.to_dict() for folder in folders_result],
        "files": {
            "items": [file_obj.to_dict() for file_obj in files_result],
            "total": total_items,
            "page": page,
            "page_size": page_size,
            "pages": total_pages,
        },
    }


def update_file(id: int, data: Dict[str, Any]) -> File:
    file_obj = File.query.get(id)
    if not file_obj:
        raise HTTPException(status_code=404, detail="File not found")

    file_obj.name = data.get("name", file_obj.name)
    file_obj.status = data.get("status", file_obj.status)
    file_obj.parent_id = data.get("parent_id", file_obj.parent_id)
    db.session.commit()

    _clear_search_cache(file_obj.uploader_id)
    return file_obj


def delete_file(id: int, commit: bool = True) -> None:
    file_obj = File.query.get(id)
    if not file_obj:
        raise HTTPException(status_code=404, detail="File not found")

    uploader_id = file_obj.uploader_id
    abs_path = file_obj.get_abs_path()
    if os.path.exists(abs_path):
        try:
            os.remove(abs_path)
        except OSError as e:
            logger.error(f"Error deleting file {abs_path}: {e}")

    db.session.delete(file_obj)
    if commit:
        db.session.commit()
        _clear_search_cache(uploader_id)


async def search_files(
    user_id: int,
    query: str,
    page: int = 1,
    page_size: int = 10,
    search_type: str = "fuzzy",
) -> Dict[str, Any]:
    if not query:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    if search_type == "vector":
        return await _search_files_vector(user_id, query, page, page_size)
    return await _search_files_fuzzy(user_id, query, page, page_size)


@cache(expire=CACHE_EXPIRATION)
async def _search_files_fuzzy(
    user_id: int, query: str, page: int, page_size: int
) -> Dict[str, Any]:
    base_query = File.query.filter(
        File.uploader_id == user_id, File.name.ilike(f"%{query}%")
    )
    total = base_query.count()
    offset = (page - 1) * page_size
    items = base_query.offset(offset).limit(page_size).all()

    return {
        "items": [file_obj.to_dict() for file_obj in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def _search_files_vector(
    user_id: int, query: str, page: int, page_size: int
) -> Dict[str, Any]:
    try:
        emb_config = get_embedding_model_config()
        embeddings = embedding_desc(query, emb_config)
        if not embeddings:
            return {
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "error": "No embeddings returned",
            }

        offset = (page - 1) * page_size
        items = (
            File.query.filter(File.uploader_id == user_id, File.vector_info.isnot(None))
            .order_by(File.vector_info.l2_distance(embeddings))
            .limit(page_size)
            .offset(offset)
            .all()
        )

        total = File.query.filter(
            File.uploader_id == user_id, File.vector_info.isnot(None)
        ).count()

        return {
            "items": [file_obj.to_dict() for file_obj in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    except Exception as e:
        logger.exception(f"Vector search error: {e}")
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "error": str(e),
        }


def _clear_search_cache(user_id: int) -> None:
    try:
        pattern = f"search:*:{user_id}:*"
        keys = list(redis_client.scan_iter(match=pattern))
        if keys:
            redis_client.delete(*keys)
    except Exception as e:
        logger.error(f"Redis clear cache error: {e}")


def get_root_file_id(user_id: int) -> Optional[int]:
    cache_key = f"user:root:file:{user_id}"
    cached_id = redis_client.get(cache_key)
    if cached_id:
        return int(cached_id)

    root_file = File.query.filter_by(uploader_id=user_id, parent_id=None).first()
    if root_file:
        redis_client.set(cache_key, root_file.id)
        return root_file.id
    return None


def upload_avatar(img_file: Union[FileStorage, Any], user_id: int) -> Dict[str, Any]:
    from app.services import folder_service, share_service, user_service

    root_folder = Folder.query.filter_by(user_id=1, parent_id=None).first()
    root_folder_id = root_folder.id if root_folder else None

    folder = Folder.query.filter_by(
        user_id=1, parent_id=root_folder_id, name="所有用户头像"
    ).first()
    if folder:
        folder_id = folder.id
    else:
        folder = folder_service.create_folder(
            {"name": "所有用户头像", "parent_id": root_folder_id, "user_id": 1}
        )
        folder_id = folder.id

    avatar = create_file(img_file, {"uploader_id": 1, "parent_id": folder_id})
    share = share_service.create_share_link(1, avatar.id)
    avatar_url = f"/api/share/{share.token}"
    user_service.update_user(user_id, {"avatar": avatar_url})

    result = avatar.to_dict()
    result["avatar_url"] = avatar_url
    return result


def batch_delete_items(items: List[Dict[str, Any]]) -> None:
    from app.services import folder_service

    for item in items:
        item_id = item.get("id")
        is_folder = item.get("is_folder", False)
        if is_folder:
            folder_service.delete_folder(item_id)
        else:
            delete_file(item_id)


def embedding_desc(desc: str, config: Dict[str, str]) -> List[float]:
    api = config.get("api")
    key = config.get("key")
    model = config.get("model", "Qwen/Qwen3-Embedding-8B")
    try:
        client = OpenAI(api_key=key, base_url=api, timeout=120)
        resp = client.embeddings.create(model=model, input=desc)
        return resp.data[0].embedding[:1536]
    except Exception as e:
        logger.exception(f"Embedding failed: {e}")
        return []


def retry_embedding(file_id: int) -> None:
    file_obj = File.query.get(file_id)
    if not file_obj:
        return
    redis_client.rpush(FILE_PROCESS_QUEUE, file_obj.id)


def rebuild_failed_indexes(user_id: Optional[int] = None) -> int:
    query = File.query.filter(File.status == "fail")
    if user_id is not None:
        query = query.filter(File.uploader_id == user_id)

    failed_files = query.all()
    if not failed_files:
        return 0

    file_ids = [f.id for f in failed_files]
    try:
        File.query.filter(File.id.in_(file_ids)).update(
            {File.status: "pending"}, synchronize_session=False
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to update file status to pending")
        return 0

    pushed = 0
    for file_id in file_ids:
        try:
            redis_client.rpush(FILE_PROCESS_QUEUE, file_id)
            pushed += 1
        except Exception:
            logger.exception(f"Failed to push file {file_id} to redis")
    return pushed


def get_all_files(user_id: int) -> List[File]:
    return File.query.filter_by(uploader_id=user_id).all()


@cache(expire=60)
async def process_status(id: int) -> Dict[str, int]:
    files = get_all_files(id)
    cnt = defaultdict(int)
    for file_obj in files:
        cnt[file_obj.status] += 1

    return {
        "处理中": cnt["pending"] + cnt["processing"],
        "成功": cnt["success"],
        "失败": cnt["fail"],
    }


def cleanup_expired_uploads(max_age_hours: int = 24) -> int:
    """清理过期的分片上传临时文件"""
    import time

    if not os.path.exists(MULTIPART_ROOT):
        return 0

    count = 0
    now = time.time()
    expiration = max_age_hours * 3600

    try:
        # 遍历用户目录
        for uploader_id_str in os.listdir(MULTIPART_ROOT):
            user_dir = os.path.join(MULTIPART_ROOT, uploader_id_str)
            if not os.path.isdir(user_dir):
                continue

            # 遍历上传任务目录
            for upload_id in os.listdir(user_dir):
                upload_dir = os.path.join(user_dir, upload_id)
                if not os.path.isdir(upload_dir):
                    continue

                try:
                    # 检查最后修改时间
                    mtime = os.path.getmtime(upload_dir)
                    if now - mtime > expiration:
                        logger.info(f"Cleaning up expired upload: {upload_dir}")
                        shutil.rmtree(upload_dir, ignore_errors=True)
                        count += 1
                except OSError as e:
                    logger.warning(f"Error accessing/deleting {upload_dir}: {e}")

            # 如果用户目录为空，也可以清理掉（可选）
            try:
                if not os.listdir(user_dir):
                    os.rmdir(user_dir)
            except OSError:
                pass

    except Exception as e:
        logger.exception(f"Error during cleanup_expired_uploads: {e}")

    return count
