"""整理 Agent 工具集：供 ReAct 调用的目录读写操作。

边界：工具内直接操作 DB/Redis，不做 LLM 决策；每个变更工具必须带 user_id 做归属校验。
会话使用独立 session 并在 finally 关闭，避免与 Agent 多步并发串 session。
"""

import logging

from langchain.tools import tool
from sqlalchemy import func

from app.extensions import SessionLocal, redis_client
from app.models.file import File
from app.models.folder import Folder

logger = logging.getLogger(__name__)


def get_session():
    """工具执行用独立 session，调用方负责 finally 关闭。"""
    return SessionLocal()


def clear_user_cache(user_id):
    """目录变更后清文件夹缓存与搜索缓存；SCAN 代替 KEYS 避免阻塞 Redis。"""
    try:
        redis_client.delete(f"user:folders:{user_id}")
        cursor = 0
        pattern = f"search:*:{user_id}:*"
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                redis_client.delete(*keys)
            if cursor == 0:
                break
    except Exception as e:
        logger.error(f"Error clearing cache for user {user_id}: {e}")


def _check_mixed_folders(session, user_id: int) -> tuple[bool, list[dict]]:
    """查找「文件+子文件夹共存」的目录（违反单一内容原则）。

    返回 (is_clean, mixed_folders_list)；用聚合计数消除 N+1。
    """
    mixed_folders: list[dict] = []

    # 根目录 parent_id 为 None，需单独判断
    has_root_subfolders = session.query(Folder).filter_by(user_id=user_id, parent_id=None).count() > 0
    has_root_files = session.query(File).filter_by(uploader_id=user_id, parent_id=None).count() > 0
    if has_root_subfolders and has_root_files:
        mixed_folders.append({"id": 0, "name": "根目录"})

    subfolder_counts = dict(
        session.query(Folder.parent_id, func.count(Folder.id))
        .filter(Folder.parent_id.isnot(None))
        .group_by(Folder.parent_id)
        .all()
    )
    file_counts = dict(
        session.query(File.parent_id, func.count(File.id))
        .filter(File.parent_id.isnot(None))
        .group_by(File.parent_id)
        .all()
    )

    folders = session.query(Folder).filter_by(user_id=user_id).all()
    for folder in folders:
        has_subfolders = subfolder_counts.get(folder.id, 0) > 0
        has_files = file_counts.get(folder.id, 0) > 0
        if has_subfolders and has_files:
            mixed_folders.append({"id": folder.id, "name": folder.name})

    return len(mixed_folders) == 0, mixed_folders


def _check_empty_folders(session, user_id: int) -> tuple[bool, list[dict]]:
    """查找空文件夹；聚合计数消除 N+1。"""
    subfolder_counts = dict(
        session.query(Folder.parent_id, func.count(Folder.id))
        .filter(Folder.parent_id.isnot(None))
        .group_by(Folder.parent_id)
        .all()
    )
    file_counts = dict(
        session.query(File.parent_id, func.count(File.id))
        .filter(File.parent_id.isnot(None))
        .group_by(File.parent_id)
        .all()
    )

    folders = session.query(Folder).filter_by(user_id=user_id).all()
    empty_folders: list[dict] = []
    for folder in folders:
        has_subfolders = subfolder_counts.get(folder.id, 0) > 0
        has_files = file_counts.get(folder.id, 0) > 0
        if not has_subfolders and not has_files:
            empty_folders.append({"id": folder.id, "name": folder.name})

    return len(empty_folders) == 0, empty_folders


def check_mixed_folders_internal(user_id: int) -> tuple[bool, str]:
    """校验单一内容原则；返回 (是否干净, 可读消息)。"""
    session = get_session()
    try:
        is_clean, mixed_folders = _check_mixed_folders(session, user_id)
        if is_clean:
            return True, "未发现既包含文件又包含子文件夹的文件夹。"
        lines = [f"ID: {f['id']}, Name: {f['name']}" for f in mixed_folders]
        return False, "以下文件夹既包含文件又包含子文件夹（违反单一内容原则）：\n" + "\n".join(lines)
    except Exception as e:
        return False, f"检查失败: {str(e)}"
    finally:
        session.close()


def check_empty_folders_internal(user_id: int) -> tuple[bool, str]:
    """校验是否仍有空文件夹。"""
    session = get_session()
    try:
        is_clean, empty_folders = _check_empty_folders(session, user_id)
        if is_clean:
            return True, "未发现空文件夹。"
        lines = [f"ID: {f['id']}, Name: {f['name']}" for f in empty_folders]
        return False, "以下文件夹是空的，请删除它们：\n" + "\n".join(lines)
    except Exception as e:
        return False, f"检查失败: {str(e)}"
    finally:
        session.close()


@tool
def get_all_files(user_id: int) -> str:
    """列出指定用户全部文件（ID/名称/父目录），供分类决策。"""
    session = get_session()
    try:
        files = session.query(File).filter_by(uploader_id=user_id).all()
        if not files:
            return "没有找到文件。"

        file_list = []
        for f in files:
            file_list.append(f"ID: {f.id}, Name: {f.name}, Parent Folder ID: {f.parent_id}")
        return "\n".join(file_list)
    except Exception as e:
        return f"获取文件列表失败: {str(e)}"
    finally:
        session.close()


@tool
def get_folder_tree(user_id: int) -> str:
    """列出指定用户全部文件夹（扁平：ID/名称/父 ID）。"""
    session = get_session()
    try:
        folders = session.query(Folder).filter_by(user_id=user_id).all()
        paths = []
        for folder in folders:
            paths.append(f"ID: {folder.id}, Name: {folder.name}, Parent ID: {folder.parent_id}")
        return "\n".join(paths)
    except Exception as e:
        return f"获取文件夹结构失败: {str(e)}"
    finally:
        session.close()


@tool
def create_folder(name: str, parent_id: int, user_id: int) -> str:
    """在指定父目录下创建文件夹；同名已存在则返回已有 ID。"""
    session = get_session()
    try:
        pid = parent_id if parent_id and parent_id != 0 else None
        existing_folder = session.query(Folder).filter_by(
            name=name,
            user_id=user_id,
            parent_id=pid
        ).first()

        if existing_folder:
            return f"文件夹 '{name}' 已存在 (ID: {existing_folder.id})"

        new_folder = Folder(name=name, user_id=user_id, parent_id=pid)
        session.add(new_folder)
        session.commit()
        session.refresh(new_folder)
        folder_id = new_folder.id
        clear_user_cache(user_id)
        return f"已创建文件夹 '{name}' (ID: {folder_id})"
    except Exception as e:
        session.rollback()
        return f"创建文件夹失败: {str(e)}"
    finally:
        session.close()


@tool
def rename_folder(folder_id: int, new_name: str, user_id: int) -> str:
    """重命名文件夹；须校验归属，且同级不重名。"""
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            return f"文件夹 {folder_id} 不存在"

        if folder.user_id != user_id:
            return f"权限错误：文件夹 {folder_id} 不属于用户 {user_id}"

        existing_folder = session.query(Folder).filter_by(
            name=new_name,
            parent_id=folder.parent_id,
            user_id=folder.user_id
        ).first()

        if existing_folder and existing_folder.id != folder_id:
            return f"重命名失败：文件夹 '{new_name}' 已存在 (ID: {existing_folder.id})。"

        folder.name = new_name
        session.commit()
        clear_user_cache(folder.user_id)
        return f"已将文件夹 {folder_id} 重命名为 '{new_name}'"
    except Exception as e:
        session.rollback()
        return f"重命名失败: {str(e)}"
    finally:
        session.close()


@tool
def move_file(file_id: int, target_folder_id: int, user_id: int) -> str:
    """移动文件到目标文件夹；校验文件与目标归属。"""
    session = get_session()
    try:
        file = session.get(File, file_id)
        if not file:
            return f"文件 {file_id} 不存在"

        if file.uploader_id != user_id:
            return f"权限错误：文件 {file_id} 不属于用户 {user_id}"

        target_id = target_folder_id if target_folder_id and target_folder_id != 0 else None

        if target_id is not None:
            target_folder = session.get(Folder, target_id)
            if not target_folder:
                return f"目标文件夹 {target_folder_id} 不存在"
            if target_folder.user_id != user_id:
                return f"权限错误：目标文件夹 {target_folder_id} 不属于用户 {user_id}"

        file.parent_id = target_id
        session.commit()
        clear_user_cache(file.uploader_id)
        return f"已将文件 {file_id} 移动到文件夹 {target_folder_id}"
    except Exception as e:
        session.rollback()
        return f"移动文件失败: {str(e)}"
    finally:
        session.close()


@tool
def get_file_information(file_id: int) -> str:
    """读取文件元数据与截断描述，供内容不明确时分类。"""
    session = get_session()
    try:
        file = session.get(File, file_id)
        if not file:
            return f"文件 {file_id} 不存在"

        description = file.description or ""
        if len(description) > 200:
            description = description[:200] + "..."

        info = {
            "id": file.id,
            "name": file.name,
            "parent_id": file.parent_id,
            "size": file.file_size,
            "mime_type": file.mime_type,
            "description": description
        }
        return str(info)
    except Exception as e:
        return f"获取文件信息失败: {str(e)}"
    finally:
        session.close()


@tool
def delete_folder(folder_id: int, user_id: int) -> str:
    """仅允许删除空文件夹；非空返回失败原因。"""
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            return f"文件夹 {folder_id} 不存在"

        if folder.user_id != user_id:
            return f"权限错误：文件夹 {folder_id} 不属于用户 {user_id}"

        subfolders_count = session.query(Folder).filter_by(parent_id=folder.id).count()
        if subfolders_count > 0:
            return f"删除失败：文件夹 {folder_id} 不为空（包含子文件夹）。"

        files_count = session.query(File).filter_by(parent_id=folder.id).count()
        if files_count > 0:
            return f"删除失败：文件夹 {folder_id} 不为空（包含文件）。"

        session.delete(folder)
        session.commit()
        clear_user_cache(folder.user_id)
        return f"已删除文件夹 {folder_id}"
    except Exception as e:
        session.rollback()
        return f"删除失败：{str(e)}"
    finally:
        session.close()


@tool
def merge_folders(source_folder_id: int, target_folder_id: int, user_id: int) -> str:
    """将源目录内容并入目标后删除源；禁止合并到自身子树，重名子目录递归合并。"""
    session = get_session()
    try:
        source = session.get(Folder, source_folder_id)
        target = session.get(Folder, target_folder_id)

        if not source or not target:
            return "源文件夹或目标文件夹不存在"

        if source.user_id != user_id:
            return f"权限错误：源文件夹 {source_folder_id} 不属于用户 {user_id}"
        if target.user_id != user_id:
            return f"权限错误：目标文件夹 {target_folder_id} 不属于用户 {user_id}"

        if source.id == target.id:
            return "不能合并同一个文件夹"

        current = target
        while current and current.parent_id:
            if current.parent_id == source.id:
                return "无法合并：目标文件夹是源文件夹的子文件夹"
            current = session.get(Folder, current.parent_id)

        files = session.query(File).filter_by(parent_id=source.id).all()
        for f in files:
            f.parent_id = target.id

        existing_subfolder_names = {
            sf.name
            for sf in session.query(Folder).filter_by(parent_id=target.id).all()
        }
        subfolders = session.query(Folder).filter_by(parent_id=source.id).all()
        for sub in subfolders:
            if sub.id == target.id:
                continue

            # 目标下同名则递归合并，避免重名冲突
            duplicate = session.query(Folder).filter_by(
                parent_id=target.id, name=sub.name
            ).first()
            if duplicate and duplicate.id != sub.id:
                _merge_folder_contents(session, sub, duplicate)
                session.delete(sub)
            else:
                sub.parent_id = target.id
                existing_subfolder_names.add(sub.name)

        session.delete(source)
        session.commit()
        clear_user_cache(source.user_id)
        return f"已将文件夹 {source_folder_id} 合并到 {target_folder_id}"
    except Exception as e:
        session.rollback()
        return f"合并失败: {str(e)}"
    finally:
        session.close()


def _merge_folder_contents(session, source: Folder, target: Folder):
    """递归把 source 内容并入 target（内部辅助，处理重名子目录）。"""
    for f in session.query(File).filter_by(parent_id=source.id).all():
        f.parent_id = target.id

    for sub in session.query(Folder).filter_by(parent_id=source.id).all():
        if sub.id == target.id:
            continue
        duplicate = session.query(Folder).filter_by(
            parent_id=target.id, name=sub.name
        ).first()
        if duplicate and duplicate.id != sub.id:
            _merge_folder_contents(session, sub, duplicate)
            session.delete(sub)
        else:
            sub.parent_id = target.id


@tool
def find_duplicate_folders(user_id: int) -> str:
    """查找同父目录下同名文件夹，便于后续 merge。"""
    session = get_session()
    try:
        folders = session.query(Folder).filter_by(user_id=user_id).all()
        grouped = {}
        for f in folders:
            key = (f.name, f.parent_id)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(f)

        duplicates = []
        for (name, parent_id), group in grouped.items():
            if len(group) > 1:
                ids = [str(f.id) for f in group]
                duplicates.append(f"Name: '{name}', Parent ID: {parent_id}, IDs: {', '.join(ids)}")

        if not duplicates:
            return "未发现重复文件夹。"

        return "发现以下重复文件夹：\n" + "\n".join(duplicates)
    except Exception as e:
        return f"查找重复文件夹失败: {str(e)}"
    finally:
        session.close()


@tool
def move_folder(folder_id: int, target_folder_id: int, user_id: int) -> str:
    """移动文件夹到目标下；禁止移入自身子树，0/None 表示根。"""
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            return f"文件夹 {folder_id} 不存在"

        if folder.user_id != user_id:
            return f"权限错误：文件夹 {folder_id} 不属于用户 {user_id}"

        if folder.id == target_folder_id:
            return "不能将文件夹移动到自身"

        if not target_folder_id or target_folder_id == 0:
            folder.parent_id = None
            session.commit()
            clear_user_cache(folder.user_id)
            return f"已将文件夹 {folder_id} 移动到根目录"

        target = session.get(Folder, target_folder_id)
        if not target:
            return f"目标文件夹 {target_folder_id} 不存在"

        if target.user_id != user_id:
            return f"权限错误：目标文件夹 {target_folder_id} 不属于用户 {user_id}"

        current = target
        while current:
            if current.id == folder.id:
                return "无法移动：目标文件夹是源文件夹的子文件夹"
            if current.parent_id:
                current = session.get(Folder, current.parent_id)
            else:
                break

        folder.parent_id = target.id
        session.commit()
        clear_user_cache(folder.user_id)
        return f"已将文件夹 {folder_id} 移动到 {target.name} (ID: {target.id})"
    except Exception as e:
        session.rollback()
        return f"移动失败: {str(e)}"
    finally:
        session.close()


@tool
def find_mixed_content_folders(user_id: int) -> str:
    """Agent 工具封装：返回违反单一内容原则的目录说明。"""
    _, message = check_mixed_folders_internal(user_id)
    return message


@tool
def find_empty_folders(user_id: int) -> str:
    """Agent 工具封装：返回空文件夹列表说明。"""
    _, message = check_empty_folders_internal(user_id)
    return message
