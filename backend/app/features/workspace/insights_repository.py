"""工作空间只读查询层。调用方持有鉴权逻辑、Session 与事务边界."""

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.file import File
from app.models.folder import Folder


@dataclass(frozen=True)
class StorageStats:
    """单个工作空间的聚合存储用量统计数据."""

    total_files: int
    total_folders: int
    total_size: int
    statuses: dict[str, int]


def storage_stats(session: Session, workspace_id: int) -> StorageStats:
    """返回文件/文件夹总数以及各索引状态的文件分布明细."""
    files = (
        session.query(
            func.count(File.id),
            func.coalesce(func.sum(File.file_size), 0),
        )
        .filter(File.workspace_id == workspace_id)
        .one()
    )
    statuses: dict[str, int] = {}
    rows = (
        session.query(File.status, func.count(File.id))
        .filter(File.workspace_id == workspace_id)
        .group_by(File.status)
        .all()
    )
    for status, count in rows:
        if status is not None:
            statuses[status] = int(count)
    folders = (
        session.query(func.count(Folder.id))
        .filter(Folder.workspace_id == workspace_id)
        .scalar()
        or 0
    )
    return StorageStats(int(files[0]), int(folders), int(files[1]), statuses)


def workspace_folders(session: Session, workspace_id: int) -> list[Folder]:
    """按主键顺序返回工作空间下的全部文件夹记录."""
    return (
        session.query(Folder)
        .filter(Folder.workspace_id == workspace_id)
        .order_by(Folder.id)
        .all()
    )
