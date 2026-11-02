"""整理 Agent 工具集: 供 ReAct 调用的目录读写操作.

边界: 工具内直接操作 DB/Redis, 不做 LLM 决策;
每个变更工具必须带 workspace_id 做归属校验.
会话使用独立 session 并在 finally 关闭, 避免与 Agent 多步并发串 session.
"""

from typing import TypedDict

from langchain.tools import tool
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError
from app.features.folder import repository as folder_repository
from app.features.folder import tree as folder_tree
from app.infra.extensions import redis_client
from app.models.file import File
from app.models.folder import Folder

from .scope import authorize_session, current_scope

SEARCH_CACHE_SCAN_COUNT = 100
MAX_DESCRIPTION_CHARS = 200


class FolderSummary(TypedDict):
    """结构校验器上报的轻量文件夹标识与状态信息."""

    id: int
    name: str


def get_session() -> Session:
    """返回工具专属会话; 调用方在 `finally` 块中负责关闭."""
    scope = current_scope()
    if scope is None:
        message = "Organize tools require a bound task identity"
        raise PermissionDeniedError(message)
    session = scope.session_factory()
    try:
        authorize_session(session, scope)
    except BaseException:
        session.close()
        raise
    else:
        return session


def clear_workspace_cache(workspace_id: int) -> None:
    """清除文件夹与搜索缓存; 使用 SCAN 代替 KEYS 避免阻塞 Redis."""
    try:
        redis_client.delete(f"workspace:folders:{workspace_id}")
        cursor = 0
        pattern = f"search:*:{workspace_id}:*"
        while True:
            cursor, keys = redis_client.scan(
                cursor=cursor,
                match=pattern,
                count=SEARCH_CACHE_SCAN_COUNT,
            )
            if keys:
                redis_client.delete(*keys)
            if cursor == 0:
                break
    except Exception as e:
        logger.error(
            "Error clearing cache for workspace {} (error_type={})",
            workspace_id,
            type(e).__name__,
        )


def _folder_child_counts(session: Session) -> dict[int, int]:
    """按父目录 ID 统计子文件夹数量."""
    rows = (
        session.query(Folder.parent_id, func.count(Folder.id))
        .filter(Folder.parent_id.isnot(None))
        .group_by(Folder.parent_id)
        .all()
    )
    return {int(parent_id): int(count) for parent_id, count in rows}


def _file_child_counts(session: Session) -> dict[int, int]:
    """按父目录 ID 统计子文件数量."""
    rows = (
        session.query(File.parent_id, func.count(File.id))
        .filter(File.parent_id.isnot(None))
        .group_by(File.parent_id)
        .all()
    )
    return {int(parent_id): int(count) for parent_id, count in rows}


def _check_mixed_folders(
    session: Session,
    workspace_id: int,
) -> tuple[bool, list[FolderSummary]]:
    """查找同时包含文件与子文件夹的目录 (违背单一内容原则).

    返回 `(is_clean, mixed_folders)`; 聚合计数避免 N+1 查询.
    """
    mixed_folders: list[FolderSummary] = []

    # 根目录 parent_id 为 None, 需单独判断
    has_root_subfolders = (
        session.query(Folder)
        .filter_by(workspace_id=workspace_id, parent_id=None)
        .count()
        > 0
    )
    has_root_files = (
        session.query(File)
        .filter_by(workspace_id=workspace_id, parent_id=None)
        .count()
        > 0
    )
    if has_root_subfolders and has_root_files:
        mixed_folders.append({"id": 0, "name": "根目录"})

    subfolder_counts = _folder_child_counts(session)
    file_counts = _file_child_counts(session)

    folders = session.query(Folder).filter_by(workspace_id=workspace_id).all()
    for folder in folders:
        has_subfolders = subfolder_counts.get(folder.id, 0) > 0
        has_files = file_counts.get(folder.id, 0) > 0
        if has_subfolders and has_files:
            mixed_folders.append({"id": folder.id, "name": folder.name})

    return len(mixed_folders) == 0, mixed_folders


def _check_empty_folders(
    session: Session,
    workspace_id: int,
) -> tuple[bool, list[FolderSummary]]:
    """查找空文件夹; 聚合计数避免 N+1 查询."""
    subfolder_counts = _folder_child_counts(session)
    file_counts = _file_child_counts(session)

    folders = session.query(Folder).filter_by(workspace_id=workspace_id).all()
    empty_folders: list[FolderSummary] = []
    for folder in folders:
        has_subfolders = subfolder_counts.get(folder.id, 0) > 0
        has_files = file_counts.get(folder.id, 0) > 0
        if not has_subfolders and not has_files:
            empty_folders.append({"id": folder.id, "name": folder.name})

    return len(empty_folders) == 0, empty_folders


def check_mixed_folders_internal(workspace_id: int) -> tuple[bool, str]:
    """校验单一内容原则; 返回 `(is_clean, message)` 元组."""
    session = get_session()
    try:
        is_clean, mixed_folders = _check_mixed_folders(session, workspace_id)
        if is_clean:
            return True, "未发现既包含文件又包含子文件夹的文件夹。"
        lines = [
            f"ID: {folder['id']}, Name: {folder['name']}" for folder in mixed_folders
        ]
        return False, "以下文件夹既包含文件又包含子文件夹 (违反单一内容原则):\n" + (
            "\n".join(lines)
        )
    except Exception:
        return False, "检查失败: 请稍后重试"
    finally:
        session.close()


def check_empty_folders_internal(workspace_id: int) -> tuple[bool, str]:
    """校验工作空间中没有遗留任何空文件夹."""
    session = get_session()
    try:
        is_clean, empty_folders = _check_empty_folders(session, workspace_id)
        if is_clean:
            return True, "未发现空文件夹。"
        lines = [
            f"ID: {folder['id']}, Name: {folder['name']}" for folder in empty_folders
        ]
        return False, "以下文件夹是空的, 请删除它们:\n" + "\n".join(lines)
    except Exception:
        return False, "检查失败: 请稍后重试"
    finally:
        session.close()


def _list_all_files(session: Session, workspace_id: int) -> str:
    """渲染工作空间下的全部文件及其父目录 ID 列表."""
    files = session.query(File).filter_by(workspace_id=workspace_id).all()
    if not files:
        return "没有找到文件。"
    return "\n".join(
        f"ID: {f.id}, Name: {f.name}, Parent Folder ID: {f.parent_id}" for f in files
    )


@tool
def get_all_files(workspace_id: int) -> str:
    """列出指定工作空间全部文件 (ID/名称/父目录), 供分类决策."""
    session = get_session()
    try:
        return _list_all_files(session, workspace_id)
    except Exception:
        return "获取文件列表失败: 请稍后重试"
    finally:
        session.close()


def _list_folder_tree(session: Session, workspace_id: int) -> str:
    """将工作空间下的全部文件夹渲染为扁平的 id/name/parent 列表."""
    folders = session.query(Folder).filter_by(workspace_id=workspace_id).all()
    return "\n".join(
        f"ID: {folder.id}, Name: {folder.name}, Parent ID: {folder.parent_id}"
        for folder in folders
    )


@tool
def get_folder_tree(workspace_id: int) -> str:
    """列出指定工作空间全部文件夹 (扁平: ID/名称/父 ID)."""
    session = get_session()
    try:
        return _list_folder_tree(session, workspace_id)
    except Exception:
        return "获取文件夹结构失败: 请稍后重试"
    finally:
        session.close()


def _create_folder(
    session: Session,
    name: str,
    parent_id: int,
    workspace_id: int,
) -> str:
    """创建文件夹, 除非已存在同名兄弟文件夹."""
    folder_repository.lock_tree(session, workspace_id)
    folder_tree.validate_name(name)
    pid = folder_tree.validate_parent(session, workspace_id, parent_id or None)
    existing_folder = (
        session.query(Folder)
        .filter_by(name=name, workspace_id=workspace_id, parent_id=pid)
        .first()
    )

    if existing_folder:
        return f"文件夹 '{name}' 已存在 (ID: {existing_folder.id})"

    new_folder = Folder(name=name, workspace_id=workspace_id, parent_id=pid)
    session.add(new_folder)
    session.commit()
    session.refresh(new_folder)
    folder_id = new_folder.id
    clear_workspace_cache(workspace_id)
    return f"已创建文件夹 '{name}' (ID: {folder_id})"


@tool
def create_folder(name: str, parent_id: int, workspace_id: int) -> str:
    """在指定父目录下创建文件夹; 同名已存在则返回已有 ID."""
    session = get_session()
    try:
        return _create_folder(session, name, parent_id, workspace_id)
    except Exception:
        session.rollback()
        return "创建文件夹失败: 请稍后重试"
    finally:
        session.close()


def _rename_folder(
    session: Session,
    folder_id: int,
    new_name: str,
    workspace_id: int,
) -> str:
    """在完成归属与兄弟重名校验后重命名文件夹."""
    folder_repository.lock_tree(session, workspace_id)
    folder = folder_repository.get_folder(session, folder_id)
    if not folder:
        return f"文件夹 {folder_id} 不存在"

    if folder.workspace_id != workspace_id:
        return f"权限错误: 文件夹 {folder_id} 不属于工作空间 {workspace_id}"

    folder_tree.assert_mutable(session, folder)
    folder_tree.validate_name(new_name)
    existing_folder = (
        session.query(Folder)
        .filter_by(
            name=new_name,
            parent_id=folder.parent_id,
            workspace_id=folder.workspace_id,
        )
        .first()
    )

    if existing_folder and existing_folder.id != folder_id:
        return f"重命名失败: 文件夹 '{new_name}' 已存在 (ID: {existing_folder.id})。"

    folder.name = new_name
    session.commit()
    clear_workspace_cache(folder.workspace_id)
    return f"已将文件夹 {folder_id} 重命名为 '{new_name}'"


@tool
def rename_folder(folder_id: int, new_name: str, workspace_id: int) -> str:
    """重命名文件夹; 须校验归属, 且同级不重名."""
    session = get_session()
    try:
        return _rename_folder(session, folder_id, new_name, workspace_id)
    except Exception:
        session.rollback()
        return "重命名失败: 请稍后重试"
    finally:
        session.close()


def _move_file(
    session: Session,
    file_id: int,
    target_folder_id: int,
    workspace_id: int,
) -> str:
    """在校验文件及目标归属后移动文件."""
    folder_repository.lock_tree(session, workspace_id)
    file = session.get(File, file_id)
    if not file:
        return f"文件 {file_id} 不存在"

    if file.workspace_id != workspace_id:
        return f"权限错误: 文件 {file_id} 不属于工作空间 {workspace_id}"

    target_id = folder_tree.validate_parent(
        session,
        workspace_id,
        target_folder_id or None,
    )

    file.parent_id = target_id
    session.commit()
    clear_workspace_cache(file.workspace_id)
    return f"已将文件 {file_id} 移动到文件夹 {target_folder_id}"


@tool
def move_file(file_id: int, target_folder_id: int, workspace_id: int) -> str:
    """移动文件到目标文件夹; 校验文件与目标归属."""
    session = get_session()
    try:
        return _move_file(session, file_id, target_folder_id, workspace_id)
    except Exception:
        session.rollback()
        return "移动文件失败: 请稍后重试"
    finally:
        session.close()


def _file_information(session: Session, file_id: int) -> str:
    """渲染文件元数据以及截断后的描述文本."""
    scope = current_scope()
    query = session.query(File).filter(File.id == file_id)
    if scope:
        query = query.filter(File.workspace_id == scope.workspace_id)
    file = query.first()
    if not file:
        return f"文件 {file_id} 不存在"

    description = file.description or ""
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS] + "..."

    info: dict[str, object] = {
        "id": file.id,
        "name": file.name,
        "parent_id": file.parent_id,
        "size": file.file_size,
        "mime_type": file.mime_type,
        "description": description,
    }
    return str(info)


@tool
def get_file_information(file_id: int) -> str:
    """读取文件元数据与截断描述, 供内容不明确时分类."""
    session = get_session()
    try:
        return _file_information(session, file_id)
    except Exception:
        return "获取文件信息失败: 请稍后重试"
    finally:
        session.close()


def _delete_empty_folder(session: Session, folder_id: int, workspace_id: int) -> str:
    """仅在文件夹既无文件也无子目录时执行删除."""
    folder_repository.lock_tree(session, workspace_id)
    folder = folder_repository.get_folder(session, folder_id)
    if not folder:
        return f"文件夹 {folder_id} 不存在"

    if folder.workspace_id != workspace_id:
        return f"权限错误: 文件夹 {folder_id} 不属于工作空间 {workspace_id}"

    folder_tree.assert_mutable(session, folder)
    subfolders_count = session.query(Folder).filter_by(parent_id=folder.id).count()
    if subfolders_count > 0:
        return f"删除失败: 文件夹 {folder_id} 不为空 (包含子文件夹)。"

    files_count = session.query(File).filter_by(parent_id=folder.id).count()
    if files_count > 0:
        return f"删除失败: 文件夹 {folder_id} 不为空 (包含文件)。"

    session.delete(folder)
    session.commit()
    clear_workspace_cache(folder.workspace_id)
    return f"已删除文件夹 {folder_id}"


@tool
def delete_folder(folder_id: int, workspace_id: int) -> str:
    """仅允许删除空文件夹; 非空返回失败原因."""
    session = get_session()
    try:
        return _delete_empty_folder(session, folder_id, workspace_id)
    except Exception:
        session.rollback()
        return "删除失败: 请稍后重试"
    finally:
        session.close()


def _merge_rejection(
    session: Session,
    scope: tuple[Folder, Folder],
    workspace_id: int,
) -> str | None:
    """当两个文件夹无法合并时返回拒绝原因字符串."""
    source, target = scope
    if source.workspace_id != workspace_id:
        return f"权限错误: 源文件夹 {source.id} 不属于工作空间 {workspace_id}"
    if target.workspace_id != workspace_id:
        return f"权限错误: 目标文件夹 {target.id} 不属于工作空间 {workspace_id}"

    folder_tree.assert_mutable(session, source)
    if source.id == target.id:
        return "不能合并同一个文件夹"

    current: Folder | None = target
    seen: set[int] = set()
    while current and current.parent_id:
        if current.id in seen:
            return "目录结构无效, 无法合并"
        seen.add(current.id)
        if current.parent_id == source.id:
            return "无法合并: 目标文件夹是源文件夹的子文件夹"
        current = session.get(Folder, current.parent_id)
    return None


def _merge_tree(
    session: Session,
    source: Folder,
    target: Folder,
    workspace_id: int,
) -> None:
    """将 `source` 的全部内容移入 `target`, 递归合并同名目录."""
    _assert_consistent_tree(session, source, workspace_id)
    _assert_consistent_tree(session, target, workspace_id)

    for file in session.query(File).filter_by(parent_id=source.id).all():
        file.parent_id = target.id

    subfolders = session.query(Folder).filter_by(parent_id=source.id).all()
    for sub in subfolders:
        if sub.id == target.id:
            continue

        # 目标下同名则递归合并, 避免重名冲突
        duplicate = (
            session.query(Folder).filter_by(parent_id=target.id, name=sub.name).first()
        )
        if duplicate and duplicate.id != sub.id:
            _merge_folder_contents(session, sub, duplicate)
            session.delete(sub)
        else:
            sub.parent_id = target.id


def _merge_folders(
    session: Session,
    folder_ids: tuple[int, int],
    workspace_id: int,
) -> str:
    """将源文件夹内容合并至目标文件夹并删除源文件夹."""
    source_folder_id, target_folder_id = folder_ids
    folder_repository.lock_tree(session, workspace_id)
    source = folder_repository.get_folder(session, source_folder_id)
    target = folder_repository.get_folder(session, target_folder_id)

    if not source or not target:
        return "源文件夹或目标文件夹不存在"

    rejection = _merge_rejection(session, (source, target), workspace_id)
    if rejection is not None:
        return rejection

    _merge_tree(session, source, target, workspace_id)
    session.delete(source)
    session.commit()
    clear_workspace_cache(source.workspace_id)
    return f"已将文件夹 {source_folder_id} 合并到 {target_folder_id}"


@tool
def merge_folders(
    source_folder_id: int,
    target_folder_id: int,
    workspace_id: int,
) -> str:
    """将源目录内容并入目标后删除源; 禁止合并到自身子树, 重名子目录递归合并."""
    session = get_session()
    try:
        return _merge_folders(
            session,
            (source_folder_id, target_folder_id),
            workspace_id,
        )
    except Exception:
        session.rollback()
        return "合并失败: 请稍后重试"
    finally:
        session.close()


def _assert_consistent_tree(session: Session, root: Folder, workspace_id: int) -> None:
    """在执行递归变更前拒绝历史遗留的跨工作空间父级关联."""
    pending = [root]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if node.id in seen or node.workspace_id != workspace_id:
            message = "Inconsistent folder tree"
            raise ValueError(message)
        seen.add(node.id)
        children = session.query(Folder).filter_by(parent_id=node.id).all()
        files = session.query(File).filter_by(parent_id=node.id).all()
        if any(file.workspace_id != workspace_id for file in files):
            message = "Inconsistent file ownership"
            raise ValueError(message)
        pending.extend(children)


def _merge_folder_contents(session: Session, source: Folder, target: Folder) -> None:
    """递归把 source 内容并入 target (内部辅助, 处理重名子目录)."""
    for file in session.query(File).filter_by(parent_id=source.id).all():
        file.parent_id = target.id

    for sub in session.query(Folder).filter_by(parent_id=source.id).all():
        if sub.id == target.id:
            continue
        duplicate = (
            session.query(Folder).filter_by(parent_id=target.id, name=sub.name).first()
        )
        if duplicate and duplicate.id != sub.id:
            _merge_folder_contents(session, sub, duplicate)
            session.delete(sub)
        else:
            sub.parent_id = target.id


def _find_duplicates(session: Session, workspace_id: int) -> str:
    """查找并渲染同父目录下共享同一名称的重名兄弟文件夹."""
    folders = session.query(Folder).filter_by(workspace_id=workspace_id).all()
    grouped: dict[tuple[str, int | None], list[Folder]] = {}
    for folder in folders:
        key = (folder.name, folder.parent_id)
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(folder)

    duplicates: list[str] = []
    for (name, parent_id), group in grouped.items():
        if len(group) > 1:
            ids = [str(folder.id) for folder in group]
            duplicates.append(
                f"Name: '{name}', Parent ID: {parent_id}, IDs: {', '.join(ids)}",
            )

    if not duplicates:
        return "未发现重复文件夹。"

    return "发现以下重复文件夹:\n" + "\n".join(duplicates)


@tool
def find_duplicate_folders(workspace_id: int) -> str:
    """查找同父目录下同名文件夹, 便于后续 merge."""
    session = get_session()
    try:
        return _find_duplicates(session, workspace_id)
    except Exception:
        return "查找重复文件夹失败: 请稍后重试"
    finally:
        session.close()


def _move_folder(
    session: Session,
    folder_id: int,
    target_folder_id: int,
    workspace_id: int,
) -> str:
    """将文件夹移动到目标目录下, 拒绝移入自身或其后代."""
    folder_repository.lock_tree(session, workspace_id)
    folder = folder_repository.get_folder(session, folder_id)
    if not folder:
        return f"文件夹 {folder_id} 不存在"

    if folder.workspace_id != workspace_id:
        return f"权限错误: 文件夹 {folder_id} 不属于工作空间 {workspace_id}"

    folder_tree.assert_mutable(session, folder)
    if folder.id == target_folder_id:
        return "不能将文件夹移动到自身"

    target_id = folder_tree.validate_parent(
        session,
        workspace_id,
        target_folder_id or None,
        moving_id=folder.id,
    )
    folder.parent_id = target_id
    session.commit()
    clear_workspace_cache(folder.workspace_id)
    if not target_folder_id:
        return f"已将文件夹 {folder_id} 移动到根目录"
    return f"已将文件夹 {folder_id} 移动到文件夹 {target_id}"


@tool
def move_folder(folder_id: int, target_folder_id: int, workspace_id: int) -> str:
    """移动文件夹到目标下; 禁止移入自身子树, 0/None 表示根."""
    session = get_session()
    try:
        return _move_folder(session, folder_id, target_folder_id, workspace_id)
    except Exception:
        session.rollback()
        return "移动失败: 请稍后重试"
    finally:
        session.close()


@tool
def find_mixed_content_folders(workspace_id: int) -> str:
    """Agent 工具封装: 返回违反单一内容原则的目录说明."""
    _, message = check_mixed_folders_internal(workspace_id)
    return message


@tool
def find_empty_folders(workspace_id: int) -> str:
    """Agent 工具封装: 返回空文件夹列表说明."""
    _, message = check_empty_folders_internal(workspace_id)
    return message
