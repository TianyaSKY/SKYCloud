"""File metadata persistence. Callers own Sessions, authorization and transactions.

No cache, storage, model provider, task publisher or independent Session is used.
Returned rows remain attached to the caller Session and must be serialized there.
"""
from dataclasses import dataclass
from collections.abc import Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.file import File
from app.models.folder import Folder
from app.models.workspace import Workspace


@dataclass(frozen=True)
class FilePage:
    items: list[File]
    total: int


@dataclass(frozen=True)
class DirectoryPage:
    folders: list[Folder]
    files: list[File]
    total: int


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_file(session: Session, file_id: int) -> File | None:
    return session.get(File, file_id)


def find_folder(session: Session, folder_id: int) -> Folder | None:
    return session.get(Folder, folder_id, populate_existing=True)


def find_reusable_file(session: Session, content_hash: str, file_size: int, workspace_id: int) -> File | None:
    return session.query(File).filter_by(content_hash=content_hash, file_size=file_size,
                                        workspace_id=workspace_id).order_by(File.id.asc()).first()


def insert_file(session: Session, file: File) -> None:
    session.add(file)
    session.flush()


def insert_files(session: Session, files: Sequence[File]) -> None:
    session.add_all(files)
    session.flush()


def remove_file(session: Session, file: File) -> None:
    session.delete(file)


def has_other_object_reference(session: Session, object_name: str, file_id: int) -> bool:
    return session.query(File.id).filter(File.file_path == object_name, File.id != file_id).first() is not None


def list_directory(session: Session, workspace_id: int, parent_id: int | None,
                   page: int, page_size: int, name: str | None, sort_by: str, order: str) -> DirectoryPage:
    folders = session.query(Folder).filter_by(workspace_id=workspace_id, parent_id=parent_id)
    files = session.query(File).filter_by(workspace_id=workspace_id, parent_id=parent_id)
    if name:
        pattern = f"%{escape_like(name)}%"
        folders = folders.filter(Folder.name.ilike(pattern, escape="\\"))
        files = files.filter(File.name.ilike(pattern, escape="\\"))
    column = {"name": File.name, "size": File.file_size}.get(sort_by, File.created_at)
    folders = folders.order_by(Folder.name.asc() if order == "asc" else Folder.name.desc())
    files = files.order_by(column.asc() if order == "asc" else column.desc())
    folder_count = folders.order_by(None).with_entities(func.count()).scalar() or 0
    file_count = files.order_by(None).with_entities(func.count()).scalar() or 0
    offset = (page - 1) * page_size
    folder_rows, file_rows = [], []
    if offset < folder_count:
        folder_rows = folders.offset(offset).limit(page_size).all()
        if len(folder_rows) < page_size:
            file_rows = files.limit(page_size - len(folder_rows)).all()
    else:
        file_rows = files.offset(offset - folder_count).limit(page_size).all()
    return DirectoryPage(folder_rows, file_rows, folder_count + file_count)


def search_names(session: Session, workspace_id: int, query: str, page: int, page_size: int) -> FilePage:
    rows = session.query(File).filter(File.workspace_id == workspace_id,
        File.name.ilike(f"%{escape_like(query)}%", escape="\\"))
    total = rows.count()
    return FilePage(rows.offset((page - 1) * page_size).limit(page_size).all(), total)


def search_vectors(session: Session, workspace_id: int, embeddings: list[float], page: int, page_size: int) -> FilePage:
    rows = session.query(File).filter(File.workspace_id == workspace_id, File.vector_info.isnot(None))
    items = rows.order_by(File.vector_info.cosine_distance(embeddings)).limit(page_size).offset((page - 1) * page_size).all()
    return FilePage(items, rows.count())


def find_root_file(session: Session, workspace_id: int) -> File | None:
    return session.query(File).filter_by(workspace_id=workspace_id, parent_id=None).first()


def find_owned_workspace(session: Session, user_id: int) -> Workspace | None:
    return session.query(Workspace).filter_by(owner_id=user_id).order_by(Workspace.id).first()


def find_child_folder(session: Session, workspace_id: int, parent_id: int | None, name: str | None = None) -> Folder | None:
    rows = session.query(Folder).filter_by(workspace_id=workspace_id, parent_id=parent_id)
    return (rows.filter_by(name=name) if name is not None else rows).first()


def failed_file_ids(session: Session, workspace_id: int | None) -> list[int]:
    rows = session.query(File.id).filter(File.status == "fail")
    if workspace_id is not None:
        rows = rows.filter(File.workspace_id == workspace_id)
    return [row.id for row in rows.all()]


def mark_pending(session: Session, file_ids: list[int]) -> None:
    session.query(File).filter(File.id.in_(file_ids)).update({File.status: "pending"}, synchronize_session=False)


def list_workspace_files(session: Session, workspace_id: int) -> list[File]:
    return session.query(File).filter_by(workspace_id=workspace_id).all()


def status_counts(session: Session, workspace_id: int) -> dict[str, int]:
    return dict(session.query(File.status, func.count(File.id)).filter(File.workspace_id == workspace_id).group_by(File.status).all())


def find_live_file(session: Session, file_id: int) -> File | None:
    """Refresh an instant-clone source after reservation commit and object locking."""
    return session.query(File).populate_existing().filter(File.id == file_id).first()


def has_object_reference(session: Session, object_name: str) -> bool:
    return session.query(File.id).filter(File.file_path == object_name).first() is not None
