import json
import logging
import mimetypes
import os
import uuid
from collections import defaultdict
from typing import List

from flask import current_app
from openai import OpenAI
from werkzeug.utils import secure_filename

from app.extensions import db, redis_client
from app.models.file import File
from app.models.folder import Folder

logger = logging.getLogger(__name__)

FILE_PROCESS_QUEUE = 'file_process_queue'
ORGANIZE_FILE_QUEUE = 'organize_file_queue'
CACHE_EXPIRATION = 3600  # 缓存过期时间（秒）


def create_file(file_obj, data):
    """
    :param file_obj:文件数据流
    :param data: 表单数据
    :return:
    """
    upload_folder = current_app.config['UPLOAD_FOLDER']

    # 保留原始文件名用于数据库存储（支持中文）
    original_filename = file_obj.filename

    # 分离文件名和后缀，确保后缀被完整保留
    base_name, extension = os.path.splitext(original_filename)

    # 安全处理文件名部分（去除中文等特殊字符）
    safe_base_name = secure_filename(base_name)

    # 生成唯一的存储文件名
    if safe_base_name:
        # 如果安全处理后还有内容，保留它
        unique_filename = f"{uuid.uuid4().hex}_{safe_base_name}{extension}"
    else:
        # 如果文件名全是中文等特殊字符，safe_base_name 会为空，直接用 UUID + 后缀
        unique_filename = f"{uuid.uuid4().hex}{extension}"

    full_path = os.path.join(upload_folder, unique_filename)

    # 保存文件到本地
    file_obj.save(full_path)
    file_size = os.path.getsize(full_path)
    if not file_obj.mimetype:
        mime_type = mimetypes.guess_type(full_path)[0]
    else:
        mime_type = file_obj.mimetype

    new_file = File(
        name=original_filename,  # 存入数据库时使用原始文件名
        file_path=unique_filename, # 只存文件名
        file_size=file_size,
        mime_type=mime_type,
        uploader_id=data.get('uploader_id'),
        parent_id=data.get('parent_id')
    )

    db.session.add(new_file)
    db.session.commit()

    # 将新文件的 ID 加入 Redis 队列
    try:
        redis_client.rpush(FILE_PROCESS_QUEUE, new_file.id)
        # 清除搜索缓存，因为有新文件添加
        _clear_search_cache(new_file.uploader_id)
    except Exception as e:
        logger.exception(f"Error pushing to Redis queue: {e}")
    return new_file


def batch_create_files(file_objs, data):
    """
    批量创建文件，优化数据库事务和Redis操作
    """
    upload_folder = current_app.config['UPLOAD_FOLDER']
    new_files = []
    uploader_id = data.get('uploader_id')

    for file_obj in file_objs:
        if not file_obj or file_obj.filename == '':
            continue

        original_filename = file_obj.filename
        base_name, extension = os.path.splitext(original_filename)
        safe_base_name = secure_filename(base_name)

        if safe_base_name:
            unique_filename = f"{uuid.uuid4().hex}_{safe_base_name}{extension}"
        else:
            unique_filename = f"{uuid.uuid4().hex}{extension}"

        full_path = os.path.join(upload_folder, unique_filename)

        # 保存文件到本地
        file_obj.save(full_path)
        file_size = os.path.getsize(full_path)
        mime_type = file_obj.mimetype or mimetypes.guess_type(full_path)[0]

        new_file = File(
            name=original_filename,
            file_path=unique_filename, # 只存文件名
            file_size=file_size,
            mime_type=mime_type,
            uploader_id=uploader_id,
            parent_id=data.get('parent_id')
        )
        new_files.append(new_file)

    if new_files:
        # 1. 批量提交数据库事务
        db.session.add_all(new_files)
        db.session.commit()

        # 2. 使用 Pipeline 批量推送 Redis，减少网络往返
        try:
            pipe = redis_client.pipeline()
            for f in new_files:
                pipe.rpush(FILE_PROCESS_QUEUE, f.id)
            pipe.execute()
            # 3. 仅清理一次缓存
            _clear_search_cache(uploader_id)
        except Exception as e:
            logger.exception(f"Error pushing to Redis queue: {e}")

    return new_files


def get_file(id) -> File:
    return File.query.get_or_404(id)


def get_files_and_folders(user_id, parent_id, page=1, page_size=10, name=None, sort_by='created_at', order='desc'):
    # 确保 page 至少为 1
    if page < 1:
        page = 1

    # 1. 构建文件夹查询
    folder_query = Folder.query.filter_by(user_id=user_id, parent_id=parent_id)
    if name:
        folder_query = folder_query.filter(Folder.name.ilike(f"%{name}%"))

    # 文件夹通常按名称排序
    if order == 'asc':
        folder_query = folder_query.order_by(Folder.name.asc())
    else:
        folder_query = folder_query.order_by(Folder.name.desc())

    # 2. 构建文件查询
    file_query = File.query.filter_by(uploader_id=user_id, parent_id=parent_id)
    if name:
        file_query = file_query.filter(File.name.ilike(f"%{name}%"))

    # 排序字段映射
    if sort_by == 'name':
        sort_column = File.name
    elif sort_by == 'size':
        sort_column = File.file_size
    else:
        sort_column = File.created_at

    if order == 'asc':
        file_query = file_query.order_by(sort_column.asc())
    else:
        file_query = file_query.order_by(sort_column.desc())

    # 3. 计算总数
    total_folders = folder_query.count()
    total_files = file_query.count()
    total_items = total_folders + total_files
    total_pages = (total_items + page_size - 1) // page_size if page_size > 0 else 1

    # 4. 计算分页偏移量
    offset = (page - 1) * page_size

    folders_result = []
    files_result = []

    if offset < total_folders:
        # 需要获取部分或全部文件夹
        folders_result = folder_query.offset(offset).limit(page_size).all()

        # 检查是否填满了当前页
        fetched_folders_count = len(folders_result)
        if fetched_folders_count < page_size:
            # 文件夹不够，需要用文件填充剩余位置
            remaining_slots = page_size - fetched_folders_count
            files_result = file_query.offset(0).limit(remaining_slots).all()
    else:
        # 已经跳过所有文件夹，只需要获取文件
        # 计算在文件列表中的偏移量
        file_offset = offset - total_folders
        files_result = file_query.offset(file_offset).limit(page_size).all()

    return {
        'folders': [folder.to_dict() for folder in folders_result],
        'files': {
            'items': [file.to_dict() for file in files_result],
            'total': total_items,  # 返回总项数（文件夹+文件），以便前端正确计算分页
            'page': page,
            'page_size': page_size,
            'pages': total_pages
        }
    }


def update_file(id, data):
    file = File.query.get_or_404(id)
    file.name = data.get('name', file.name)
    file.status = data.get('status', file.status)
    file.parent_id = data.get('parent_id', file.parent_id)
    db.session.commit()

    # 清除搜索缓存
    _clear_search_cache(file.uploader_id)

    return file


def delete_file(id, commit=True):
    file = File.query.get_or_404(id)
    uploader_id = file.uploader_id

    # 删除本地文件
    abs_path = file.get_abs_path()
    if os.path.exists(abs_path):
        try:
            os.remove(abs_path)
        except OSError as e:
            logger.error(f"Error deleting file {abs_path}: {e}")

    # 数据库记录的删除会由 SQLAlchemy 的 cascade 机制自动处理关联的 Share 记录
    db.session.delete(file)

    if commit:
        db.session.commit()
        # 清除搜索缓存
        _clear_search_cache(uploader_id)


def search_files(user_id, query, page=1, page_size=10, search_type='fuzzy'):
    """
    搜索文件，支持模糊搜索和向量搜索
    """
    if not query:
        return {'items': [], 'total': 0, 'page': page, 'page_size': page_size}

    if search_type == 'vector':
        return _search_files_vector(user_id, query, page, page_size)
    else:
        return _search_files_fuzzy(user_id, query, page, page_size)


def _search_files_fuzzy(user_id, query, page, page_size):
    """
    数据库模糊搜索
    """
    cache_key = f"search:fuzzy:{user_id}:{query}:{page}:{page_size}"

    # 尝试从 Redis 获取缓存
    try:
        cached_result = redis_client.get(cache_key)
        if cached_result:
            logger.info(f"Cache hit for query: {query}")
            return json.loads(cached_result)
    except Exception as e:
        logger.error(f"Redis error: {e}")

    # 缓存未命中，查询数据库
    logger.info(f"Cache miss for query: {query}")
    pagination = File.query.filter(
        File.uploader_id == user_id,
        File.name.ilike(f"%{query}%")
    ).paginate(page=page, per_page=page_size, error_out=False)

    results = {
        'items': [file.to_dict() for file in pagination.items],
        'total': pagination.total,
        'page': page,
        'page_size': page_size
    }

    # 将结果存入 Redis 缓存
    try:
        redis_client.setex(cache_key, CACHE_EXPIRATION, json.dumps(results))
    except Exception as e:
        logger.exception(f"Redis set error: {e}")

    return results


def _search_files_vector(user_id, query, page, page_size):
    """
    向量化搜索
    """
    try:
        from app.services import sys_dict_service
        # 调用远程服务获取 embedding
        emb_url = sys_dict_service.get_sys_dict_by_key("emb_api_url").value
        emb_key = sys_dict_service.get_sys_dict_by_key("emb_api_key").value
        emb_model = sys_dict_service.get_sys_dict_by_key("emb_model_name").value
        embeddings = embedding_desc(query, emb_url, emb_key, emb_model)

        if not embeddings:
            return {'items': [], 'total': 0, 'page': page, 'page_size': page_size, 'error': 'No embeddings returned'}

        query_embedding = embeddings

        # 使用 pgvector 进行相似度搜索
        # 计算偏移量
        offset = (page - 1) * page_size

        files = File.query.filter(
            File.uploader_id == user_id,
            File.vector_info.isnot(None)
        ).order_by(
            File.vector_info.l2_distance(query_embedding)
        ).limit(page_size).offset(offset).all()

        # 获取总数
        total = File.query.filter(
            File.uploader_id == user_id,
            File.vector_info.isnot(None)
        ).count()

        results = {
            'items': [file.to_dict() for file in files],
            'total': total,
            'page': page,
            'page_size': page_size
        }

        return results

    except Exception as e:
        logger.exception(f"Vector search error: {e}")
        # 降级为模糊搜索或返回空
        return {'items': [], 'total': 0, 'page': page, 'page_size': page_size, 'error': str(e)}


def _clear_search_cache(user_id):
    """
    清除指定用户的搜索缓存
    """
    try:
        # 查找该用户的所有搜索缓存键
        # 优化：使用 scan_iter 替代 keys，避免在大数据量下阻塞 Redis
        pattern = f"search:*:{user_id}:*"
        # 收集所有匹配的 key (如果 key 非常多，建议分批 delete)
        keys = list(redis_client.scan_iter(match=pattern))
        if keys:
            redis_client.delete(*keys)
    except Exception as e:
        logger.error(f"Redis clear cache error: {e}")


def get_root_file_id(user_id):
    cache_key = f"user:root:file:{user_id}"
    cached_id = redis_client.get(cache_key)
    if cached_id:
        return int(cached_id)

    root_file = File.query.filter_by(uploader_id=user_id, parent_id=None).first()
    if root_file:
        redis_client.set(cache_key, root_file.id)
        return root_file.id
    return None


def upload_avatar(img_file, user_id):
    from app.services import folder_service, share_service, user_service

    root_folder_id = folder_service.get_root_folder_id(1)

    folder = Folder.query.filter_by(user_id=1, parent_id=root_folder_id, name="所有用户头像").first()

    if folder:
        folder_id = folder.id
    else:
        folder = folder_service.create_folder({"name": "所有用户头像", "parent_id": root_folder_id, "user_id": 1})
        folder_id = folder.id

    avatar: File = create_file(img_file, {"uploader_id": 1, "parent_id": folder_id})
    # 为其生成URL
    share = share_service.create_share_link(1, avatar.id)
    avatar_url = f"/api/share/{share.token}"

    # 存入数据库并刷新缓存
    user_service.update_user(user_id, {"avatar": avatar_url})

    result = avatar.to_dict()
    result['avatar_url'] = avatar_url
    return result


def batch_delete_items(items: List[dict]):
    from app.services import folder_service
    for item in items:
        item_id = item.get('id')
        is_folder = item.get('is_folder', False)
        if is_folder:
            folder_service.delete_folder(item_id)
        else:
            delete_file(item_id)
    return None


def embedding_desc(desc, api, key, model="Qwen/Qwen3-Embedding-8B"):
    try:
        client = OpenAI(api_key=key, base_url=api, timeout=120)
        resp = client.embeddings.create(model=model, input=desc)
        return resp.data[0].embedding[:1536]
    except Exception as e:
        logger.exception(f"Embedding failed: {e}")
        return []


def retry_embedding(file_id):
    file = File.query.get(file_id)
    if not file:
        return
    redis_client.rpush(FILE_PROCESS_QUEUE, file.id)


def rebuild_failed_indexes(user_id=None):
    """
    批量重建索引失败的文件
    """
    query = File.query.filter(File.status == 'fail')
    if user_id is not None:
        query = query.filter(File.uploader_id == user_id)

    failed_files = query.all()
    if not failed_files:
        return 0

    file_ids = [f.id for f in failed_files]

    try:
        # 先更新数据库状态（批量）
        File.query.filter(File.id.in_(file_ids)) \
            .update({File.status: 'pending'}, synchronize_session=False)

        db.session.commit()

    except Exception:
        db.session.rollback()
        logger.exception("Failed to update file status to pending")
        return 0

    # 再推送到 Redis（允许部分失败）
    pushed = 0
    for file_id in file_ids:
        try:
            redis_client.rpush(FILE_PROCESS_QUEUE, file_id)
            pushed += 1
        except Exception:
            logger.exception(f"Failed to push file {file_id} to redis")

    return pushed


def get_all_files(user_id):
    return File.query.filter_by(uploader_id=user_id).all()


def process_status(id):
    """获取所有文件的处理状态，使用 Redis 缓存加速"""
    cache_key = f"file:status:count:{id}"

    try:
        cached_data = redis_client.get(cache_key)
        if cached_data:
            return json.loads(cached_data)
    except Exception as e:
        logger.error(f"Redis error in process_status: {e}")

    # 缓存未命中，查询数据库
    files = get_all_files(id)
    cnt = defaultdict(int)
    for file in files:
        cnt[file.status] += 1

    result = {
        "处理中": cnt['pending'] + cnt['processing'],
        "成功": cnt['success'],
        "失败": cnt['fail'],
    }

    # 存入 Redis，设置较短的过期时间（如 60 秒）
    try:
        redis_client.setex(cache_key, 60, json.dumps(result))
    except Exception as e:
        logger.error(f"Redis set error in process_status: {e}")

    return result