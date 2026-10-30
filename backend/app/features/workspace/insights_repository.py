"""Workspace read queries. Caller owns authorization, Session and transaction."""

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.file import File
from app.models.folder import Folder


@dataclass(frozen=True)
class StorageStats:
    """Aggregate storage usage for one workspace."""

    total_files: int
    total_folders: int
    total_size: int
    statuses: dict[str, int]


def storage_stats(session: Session, workspace_id: int) -> StorageStats:
    """Return file/folder counts and the per-status file breakdown."""
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
    """Return every folder of the workspace ordered by identity."""
    return (
        session.query(Folder)
        .filter(Folder.workspace_id == workspace_id)
        .order_by(Folder.id)
        .all()
    )
