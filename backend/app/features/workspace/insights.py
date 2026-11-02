"""工作空间协议适配器共享的只读鉴权数据模型."""

from typing import TYPE_CHECKING, NotRequired, TypedDict

from sqlalchemy.orm import Session

from app.features.workspace import insights_repository as repository
from app.features.workspace.permissions import assert_member

if TYPE_CHECKING:
    from app.models.folder import Folder

KIBIBYTE = 1024
MAX_FOLDER_DEPTH = 10
STATUS_KEYS = ("success", "pending", "processing", "fail")


class FolderNode(TypedDict):
    """工作空间嵌套目录树中的单个文件夹节点."""

    id: int
    name: str
    children: NotRequired[list["FolderNode"]]


class StorageOverview(TypedDict):
    """单个工作空间的存储用量统计 (包含原始字节数与人类可读格式)."""

    total_files: int
    total_folders: int
    total_size_bytes: int
    total_size_human: str
    status_breakdown: dict[str, int]


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < KIBIBYTE:
            return f"{value:.1f} {unit}"
        value /= KIBIBYTE
    return f"{value:.1f} PB"


def storage_overview(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> StorageOverview:
    """为工作空间成员返回该空间的存储用量汇总数据."""
    assert_member(session, workspace_id, user_id)
    stats = repository.storage_stats(session, workspace_id)
    return StorageOverview(
        total_files=stats.total_files,
        total_folders=stats.total_folders,
        total_size_bytes=stats.total_size,
        total_size_human=_human_size(stats.total_size),
        status_breakdown={
            status: stats.statuses.get(status, 0) for status in STATUS_KEYS
        },
    )


def folder_tree(
    session: Session,
    workspace_id: int,
    user_id: int,
    max_depth: int = 3,
) -> list[FolderNode]:
    """返回受 ``max_depth`` 深度约束的工作空间目录树."""
    assert_member(session, workspace_id, user_id)
    depth = max(1, min(max_depth, MAX_FOLDER_DEPTH))
    children: dict[int | None, list[Folder]] = {}
    for folder in repository.workspace_folders(session, workspace_id):
        children.setdefault(folder.parent_id, []).append(folder)

    def build(parent_id: int | None, current_depth: int) -> list[FolderNode]:
        if current_depth > depth:
            return []
        nodes: list[FolderNode] = []
        for folder in children.get(parent_id, []):
            node = FolderNode(id=int(folder.id), name=folder.name)
            descendants = build(folder.id, current_depth + 1)
            if descendants:
                node["children"] = descendants
            nodes.append(node)
        return nodes

    return build(None, 1)
