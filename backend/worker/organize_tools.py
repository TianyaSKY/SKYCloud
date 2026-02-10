import logging

from langchain.tools import tool

from app.extensions import db, redis_client
from app.models.file import File
from app.models.folder import Folder

logger = logging.getLogger(__name__)


def get_session():
    """Create a new independent session for tool execution"""
    return db.session()


def clear_user_cache(user_id):
    """Clear user related caches"""
    try:
        # Clear folder cache
        redis_client.delete(f"user:folders:{user_id}")
        # Clear search cache
        keys = redis_client.keys(f"search:*:{user_id}:*")
        if keys:
            redis_client.delete(*keys)
    except Exception as e:
        logger.error(f"Error clearing cache for user {user_id}: {e}")


def check_mixed_folders_internal(user_id: int) -> str:
    """
    内部使用的检查逻辑。
    """
    session = get_session()
    try:
        folders = session.query(Folder).filter_by(user_id=user_id).all()
        mixed_folders = []

        # 检查根目录 (parent_id 为 None)
        has_root_subfolders = session.query(Folder).filter_by(user_id=user_id, parent_id=None).count() > 0
        has_root_files = session.query(File).filter_by(uploader_id=user_id, parent_id=None).count() > 0
        if has_root_subfolders and has_root_files:
            mixed_folders.append("ID: 0, Name: 根目录")

        for folder in folders:
            has_subfolders = session.query(Folder).filter_by(parent_id=folder.id).count() > 0
            has_files = session.query(File).filter_by(parent_id=folder.id).count() > 0

            if has_subfolders and has_files:
                mixed_folders.append(f"ID: {folder.id}, Name: {folder.name}")

        if not mixed_folders:
            return "未发现既包含文件又包含子文件夹的文件夹。"

        return "以下文件夹既包含文件又包含子文件夹（违反单一内容原则）：\n" + "\n".join(mixed_folders)
    except Exception as e:
        return f"检查失败: {str(e)}"
    finally:
        session.close()


def check_empty_folders_internal(user_id: int) -> str:
    """
    内部使用的检查逻辑，查找空文件夹。
    """
    session = get_session()
    try:
        folders = session.query(Folder).filter_by(user_id=user_id).all()
        empty_folders = []

        for folder in folders:
            has_subfolders = session.query(Folder).filter_by(parent_id=folder.id).count() > 0
            has_files = session.query(File).filter_by(parent_id=folder.id).count() > 0

            if not has_subfolders and not has_files:
                empty_folders.append(f"ID: {folder.id}, Name: {folder.name}")

        if not empty_folders:
            return "未发现空文件夹。"

        return "以下文件夹是空的，请删除它们：\n" + "\n".join(empty_folders)
    except Exception as e:
        return f"检查失败: {str(e)}"
    finally:
        session.close()


@tool
def get_all_files(user_id: int) -> str:
    """
    获取指定用户的所有文件列表。
    """
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
    """
    获取指定用户的文件夹树结构。
    """
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
    """
    创建新文件夹。
    """
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
def rename_folder(folder_id: int, new_name: str) -> str:
    """
    重命名文件夹。
    """
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            return f"文件夹 {folder_id} 不存在"

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
def move_file(file_id: int, target_folder_id: int) -> str:
    """
    将文件移动到目标文件夹。
    """
    session = get_session()
    try:
        file = session.get(File, file_id)
        if not file:
            return f"文件 {file_id} 不存在"

        target_id = target_folder_id if target_folder_id and target_folder_id != 0 else None
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
    """
    获取文件的详细信息。
    """
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
def delete_folder(folder_id: int) -> str:
    """
    删除文件夹。注意：只能删除空文件夹。
    """
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            return f"文件夹 {folder_id} 不存在"

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
def merge_folders(source_folder_id: int, target_folder_id: int) -> str:
    """
    将源文件夹的所有内容移动到目标文件夹，并删除源文件夹。
    """
    session = get_session()
    try:
        source = session.get(Folder, source_folder_id)
        target = session.get(Folder, target_folder_id)

        if not source or not target:
            return "源文件夹或目标文件夹不存在"

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

        subfolders = session.query(Folder).filter_by(parent_id=source.id).all()
        for sub in subfolders:
            if sub.id != target.id:
                sub.parent_id = target.id

        session.delete(source)
        session.commit()
        clear_user_cache(source.user_id)
        return f"已将文件夹 {source_folder_id} 合并到 {target_folder_id}"
    except Exception as e:
        session.rollback()
        return f"合并失败: {str(e)}"
    finally:
        session.close()


@tool
def find_duplicate_folders(user_id: int) -> str:
    """
    查找具有相同名称和相同父文件夹的重复文件夹。
    """
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
def move_folder(folder_id: int, target_folder_id: int) -> str:
    """
    将文件夹移动到目标文件夹中（作为子文件夹）。
    """
    session = get_session()
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            return f"文件夹 {folder_id} 不存在"

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
    """
    查找既包含文件又包含子文件夹的文件夹。
    """
    return check_mixed_folders_internal(user_id)


@tool
def find_empty_folders(user_id: int) -> str:
    """
    查找所有空文件夹。
    """
    return check_empty_folders_internal(user_id)
