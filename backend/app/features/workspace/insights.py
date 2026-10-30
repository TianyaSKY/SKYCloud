"""Authorized read models shared by workspace protocol adapters."""

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
    """One folder in the nested workspace tree."""

    id: int
    name: str
    children: NotRequired[list["FolderNode"]]


class StorageOverview(TypedDict):
    """Storage usage of one workspace, raw and human readable."""

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
    """Return storage totals for one member of the workspace."""
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
    """Return the workspace folder tree bounded by ``max_depth``."""
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
