"""Fresh file scope and row locking for share publication."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.file import File


def file_workspace_id(session: Session, file_id: int) -> int | None:
    """Read the live workspace scope of a file without flushing."""
    return session.scalar(
        select(File.workspace_id)
        .where(File.id == file_id)
        .execution_options(autoflush=False),
    )


def lock_scoped_file(session: Session, file_id: int, workspace_id: int) -> File | None:
    """Lock one file row, proving it still belongs to the workspace."""
    return session.scalar(
        select(File)
        .where(File.id == file_id, File.workspace_id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True),
    )
