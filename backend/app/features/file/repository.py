"""File metadata persistence. Callers own Sessions, authorization and transactions.

No cache, storage, model provider, task publisher or independent Session is used.
Returned rows remain attached to the caller Session and must be serialized there.
"""

from collections.abc import Callable, Sequence
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
    """One page of file rows with the total row count."""

    items: list[File]
    total: int


@dataclass(frozen=True, slots=True)
class DirectoryPage:
    """Directory page holding folders first, then files, with the combined total."""

    folders: list[Folder]
    files: list[File]
    total: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FileRecordSpec:
    """Every input required to publish one file row."""

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
    before_commit: Callable[[File], None] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ListDirectoryParams:
    """Directory listing filters, ordering and paging."""

    workspace_id: int
    parent_id: int | None
    page: int
    page_size: int
    name: str | None
    sort_by: str
    order: str


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so the term matches literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_file(session: Session, file_id: int) -> File | None:
    """Return one file row by primary key."""
    return session.get(File, file_id)


def find_folder(session: Session, folder_id: int) -> Folder | None:
    """Return one folder row with refreshed identity state."""
    return session.get(Folder, folder_id, populate_existing=True)


def find_reusable_file(
    session: Session, content_hash: str, file_size: int, workspace_id: int,
) -> File | None:
    """Return the oldest row with the same hash and size in the workspace."""
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
    """Add and flush one new file row."""
    session.add(file)
    session.flush()


def insert_files(session: Session, files: Sequence[File]) -> None:
    """Add and flush several new file rows."""
    session.add_all(files)
    session.flush()


def remove_file(session: Session, file: File) -> None:
    """Mark one attached file row for deletion at commit time."""
    session.delete(file)


def has_other_object_reference(
    session: Session, object_name: str, file_id: int,
) -> bool:
    """Return whether another row still references the object key."""
    existing = session.scalar(
        select(File.id)
        .where(File.file_path == object_name, File.id != file_id)
        .limit(1),
    )
    return existing is not None


def list_directory(
    session: Session, params: ListDirectoryParams,
) -> DirectoryPage:
    """Return one folder-then-file page of the requested directory."""
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
    """Return one page of files whose name matches the term."""
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
    """Return one page of embedded files ordered by cosine distance."""
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
    """Return the first root-level file of the workspace."""
    return session.scalars(
        select(File)
        .where(File.workspace_id == workspace_id, File.parent_id.is_(None))
        .limit(1),
    ).first()


def find_owned_workspace(session: Session, user_id: int) -> Workspace | None:
    """Return the oldest workspace owned by the user."""
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
    """Return the first child folder, optionally filtered by name."""
    query = select(Folder).where(
        Folder.workspace_id == workspace_id, Folder.parent_id == parent_id,
    )
    if name is not None:
        query = query.where(Folder.name == name)
    return session.scalars(query.limit(1)).first()


def failed_file_ids(session: Session, workspace_id: int | None) -> list[int]:
    """Return the ids of files whose indexing failed."""
    query = select(File.id).where(File.status == "fail")
    if workspace_id is not None:
        query = query.where(File.workspace_id == workspace_id)
    return list(session.scalars(query))


def mark_pending(session: Session, file_ids: list[int]) -> None:
    """Reset the given rows to the pending indexing status."""
    session.execute(
        update(File)
        .where(File.id.in_(file_ids))
        .values({File.status: "pending"})
        .execution_options(synchronize_session=False),
    )


def list_workspace_files(session: Session, workspace_id: int) -> list[File]:
    """Return every file row of the workspace."""
    return list(
        session.scalars(select(File).where(File.workspace_id == workspace_id)),
    )


def status_counts(session: Session, workspace_id: int) -> dict[str, int]:
    """Count files per indexing status inside the workspace."""
    rows = session.execute(
        select(File.status, func.count(File.id))
        .where(File.workspace_id == workspace_id)
        .group_by(File.status),
    )
    return {status: count for status, count in rows if status is not None}


def find_live_file(session: Session, file_id: int) -> File | None:
    """Refresh an instant-clone source after reservation commit and object locking."""
    return session.scalars(
        select(File)
        .where(File.id == file_id)
        .execution_options(populate_existing=True),
    ).first()


def has_object_reference(session: Session, object_name: str) -> bool:
    """Return whether any file row references the object key."""
    existing = session.scalar(
        select(File.id).where(File.file_path == object_name).limit(1),
    )
    return existing is not None
