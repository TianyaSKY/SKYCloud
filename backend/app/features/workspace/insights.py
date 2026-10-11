"""Authorized read models shared by workspace protocol adapters."""
from sqlalchemy.orm import Session

from app.features.workspace import insights_repository as repository
from app.features.workspace.permissions import assert_member


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def storage_overview(session: Session, workspace_id: int, user_id: int) -> dict:
    assert_member(session, workspace_id, user_id)
    stats = repository.storage_stats(session, workspace_id)
    return {
        "total_files": stats.total_files, "total_folders": stats.total_folders,
        "total_size_bytes": stats.total_size, "total_size_human": _human_size(stats.total_size),
        "status_breakdown": {status: stats.statuses.get(status, 0)
                             for status in ("success", "pending", "processing", "fail")},
    }


def folder_tree(session: Session, workspace_id: int, user_id: int, max_depth: int = 3) -> list[dict]:
    assert_member(session, workspace_id, user_id)
    depth = max(1, min(max_depth, 10))
    children: dict[int | None, list] = {}
    for folder in repository.workspace_folders(session, workspace_id):
        children.setdefault(folder.parent_id, []).append(folder)

    def build(parent_id, current_depth):
        if current_depth > depth:
            return []
        nodes = []
        for folder in children.get(parent_id, []):
            node = {"id": folder.id, "name": folder.name}
            descendants = build(folder.id, current_depth + 1)
            if descendants:
                node["children"] = descendants
            nodes.append(node)
        return nodes
    return build(None, 1)
