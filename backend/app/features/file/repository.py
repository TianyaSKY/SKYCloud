"""文件元数据持久化层。调用方持有 Session、鉴权逻辑与事务边界.

本层不直接访问缓存、对象存储、模型供应商、任务发布者或独立 Session。
返回的实体行仍附加在调用方 Session 上, 须在该会话内序列化。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from app.models.file import File, VectorValue
from app.models.folder import Folder
from app.models.workspace import Workspace

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class FilePage:
    """包含单页文件记录与匹配总行数的分页结构."""

    items: list[File]
    total: int


@dataclass(frozen=True, slots=True)
class DirectoryPage:
    """目录浏览分页结果: 文件夹优先排列, 其后为文件, 附带总数."""

    folders: list[Folder]
    files: list[File]
    total: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FileRecordSpec:
    """发布单条文件记录所需的全部入参字段."""

    name: str
    file_path: str
    file_size: int
    mime_type: str | None
    workspace_id: int
    uploader_id: int | None
    parent_id: int | None
    content_hash: str | None
    status: str = "pending"
    description: str | None = None
    vector_info: VectorValue | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ListDirectoryParams:
    """目录浏览过滤参数、排序规则与分页规格."""

    workspace_id: int
    parent_id: int | None
    page: int
    page_size: int
    name: str | None
    sort_by: str
    order: str


def escape_like(value: str) -> str:
    """转义 LIKE 通配符, 使检索词按字面量精确匹配."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_file(session: Session, file_id: int) -> File | None:
    """根据主键 ID 查询单条文件记录."""
    return session.get(File, file_id)


def find_folder(session: Session, folder_id: int) -> Folder | None:
    """查询单条文件夹记录并刷新其会话状态."""
    return session.get(Folder, folder_id, populate_existing=True)


def find_reusable_file(
    session: Session, content_hash: str, file_size: int, workspace_id: int,
) -> File | None:
    """查询工作空间内具有相同哈希值与文件大小的最早历史记录."""
    return session.scalars(
        select(File)
        .where(
            File.content_hash == content_hash,
            File.file_size == file_size,
            File.workspace_id == workspace_id,
        )
        .order_by(File.id.asc())
        .limit(1),
    ).first()


def insert_file(session: Session, file: File) -> None:
    """新增并 flush 单条文件记录."""
    session.add(file)
    session.flush()


def insert_files(session: Session, files: Sequence[File]) -> None:
    """批量新增并 flush 多条文件记录."""
    session.add_all(files)
    session.flush()


def remove_file(session: Session, file: File) -> None:
    """标记单条已附加的文件记录以便在提交时删除."""
    session.delete(file)


def has_other_object_reference(
    session: Session, object_name: str, file_id: int,
) -> bool:
    """查询是否仍有其他文件行引用同一对象存储键."""
    existing = session.scalar(
        select(File.id)
        .where(File.file_path == object_name, File.id != file_id)
        .limit(1),
    )
    return existing is not None


def list_directory(
    session: Session, params: ListDirectoryParams,
) -> DirectoryPage:
    """返回目标目录下文件夹置顶的分页目录浏览结果."""
    folder_filters: tuple[ColumnElement[bool], ...] = (
        Folder.workspace_id == params.workspace_id,
        Folder.parent_id == params.parent_id,
    )
    file_filters: tuple[ColumnElement[bool], ...] = (
        File.workspace_id == params.workspace_id,
        File.parent_id == params.parent_id,
    )
    if params.name:
        pattern = f"%{escape_like(params.name)}%"
        folder_filters += (Folder.name.ilike(pattern, escape="\\"),)
        file_filters += (File.name.ilike(pattern, escape="\\"),)

    ascending = params.order == "asc"
    folder_order = Folder.name.asc() if ascending else Folder.name.desc()
    file_order: (
        ColumnElement[str]
        | ColumnElement[int | None]
        | ColumnElement[datetime | None]
    )
    if params.sort_by == "name":
        file_order = File.name.asc() if ascending else File.name.desc()
    elif params.sort_by == "size":
        file_order = File.file_size.asc() if ascending else File.file_size.desc()
    else:
        file_order = File.created_at.asc() if ascending else File.created_at.desc()

    folder_count = session.scalar(
        select(func.count()).select_from(Folder).where(*folder_filters),
    ) or 0
    file_count = session.scalar(
        select(func.count()).select_from(File).where(*file_filters),
    ) or 0

    offset = (params.page - 1) * params.page_size
    folder_rows: list[Folder] = []
    file_rows: list[File] = []
    if offset < folder_count:
        folder_rows = list(
            session.scalars(
                select(Folder)
                .where(*folder_filters)
                .order_by(folder_order)
                .offset(offset)
                .limit(params.page_size),
            ),
        )
        if len(folder_rows) < params.page_size:
            remaining = params.page_size - len(folder_rows)
            file_rows = list(
                session.scalars(
                    select(File)
                    .where(*file_filters)
                    .order_by(file_order)
                    .limit(remaining),
                ),
            )
    else:
        file_rows = list(
            session.scalars(
                select(File)
                .where(*file_filters)
                .order_by(file_order)
                .offset(offset - folder_count)
                .limit(params.page_size),
            ),
        )
    return DirectoryPage(folder_rows, file_rows, folder_count + file_count)


def search_names(
    session: Session, workspace_id: int, query: str, page: int, page_size: int,
) -> FilePage:
    """返回文件名与关键词匹配的单页文件记录."""
    filters = (
        File.workspace_id == workspace_id,
        File.name.ilike(f"%{escape_like(query)}%", escape="\\"),
    )
    total = session.scalar(
        select(func.count()).select_from(File).where(*filters),
    ) or 0
    items = list(
        session.scalars(
            select(File)
            .where(*filters)
            .offset((page - 1) * page_size)
            .limit(page_size),
        ),
    )
    return FilePage(items, total)


def search_vectors(
    session: Session,
    workspace_id: int,
    embeddings: Sequence[float],
    page: int,
    page_size: int,
) -> FilePage:
    """按余弦距离升序返回已建立向量索引的单页文件列表."""
    filters = (
        File.workspace_id == workspace_id,
        File.vector_info.isnot(None),
    )
    total = session.scalar(
        select(func.count()).select_from(File).where(*filters),
    ) or 0
    items = list(
        session.scalars(
            select(File)
            .where(*filters)
            .order_by(File.vector_info.cosine_distance(embeddings))
            .limit(page_size)
            .offset((page - 1) * page_size),
        ),
    )
    return FilePage(items, total)


def find_root_file(session: Session, workspace_id: int) -> File | None:
    """返回工作空间根目录下的首个文件记录."""
    return session.scalars(
        select(File)
        .where(File.workspace_id == workspace_id, File.parent_id.is_(None))
        .limit(1),
    ).first()


def find_owned_workspace(session: Session, user_id: int) -> Workspace | None:
    """返回该用户拥有的最早创建的工作空间."""
    return session.scalars(
        select(Workspace)
        .where(Workspace.owner_id == user_id)
        .order_by(Workspace.id)
        .limit(1),
    ).first()


def find_child_folder(
    session: Session,
    workspace_id: int,
    parent_id: int | None,
    name: str | None = None,
) -> Folder | None:
    """返回首个子文件夹记录, 可选按名称进行过滤."""
    query = select(Folder).where(
        Folder.workspace_id == workspace_id, Folder.parent_id == parent_id,
    )
    if name is not None:
        query = query.where(Folder.name == name)
    return session.scalars(query.limit(1)).first()


def failed_file_ids(session: Session, workspace_id: int | None) -> list[int]:
    """返回索引构建失败的文件 ID 列表."""
    query = select(File.id).where(File.status == "fail")
    if workspace_id is not None:
        query = query.where(File.workspace_id == workspace_id)
    return list(session.scalars(query))


def mark_pending(session: Session, file_ids: list[int]) -> None:
    """将指定文件记录的状态重置为 pending 待索引状态."""
    session.execute(
        update(File)
        .where(File.id.in_(file_ids))
        .values({File.status: "pending"})
        .execution_options(synchronize_session=False),
    )


def list_workspace_files(session: Session, workspace_id: int) -> list[File]:
    """返回工作空间下的全部文件记录行."""
    return list(
        session.scalars(select(File).where(File.workspace_id == workspace_id)),
    )


def status_counts(session: Session, workspace_id: int) -> dict[str, int]:
    """按索引状态统计工作空间内的文件数量分布."""
    rows = session.execute(
        select(File.status, func.count(File.id))
        .where(File.workspace_id == workspace_id)
        .group_by(File.status),
    )
    return {status: count for status, count in rows if status is not None}


def find_live_file(session: Session, file_id: int) -> File | None:
    """在预留提交及对象锁定后刷新秒传源文件状态."""
    return session.scalars(
        select(File)
        .where(File.id == file_id)
        .execution_options(populate_existing=True),
    ).first()


def has_object_reference(session: Session, object_name: str) -> bool:
    """查询是否有任意文件记录引用了该对象存储键."""
    existing = session.scalar(
        select(File.id).where(File.file_path == object_name).limit(1),
    )
    return existing is not None
