"""Fresh file scope and row locking for share publication."""
from sqlalchemy import select
from app.models.file import File


def file_workspace_id(session, file_id):
    return session.scalar(select(File.workspace_id).where(File.id == file_id)
                          .execution_options(autoflush=False))


def lock_scoped_file(session, file_id, workspace_id):
    return session.scalar(select(File).where(File.id == file_id, File.workspace_id == workspace_id)
                          .with_for_update().execution_options(populate_existing=True))
