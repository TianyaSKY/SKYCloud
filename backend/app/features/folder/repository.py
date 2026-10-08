"""Fresh folder reads and transaction-scoped serialization of tree mutations."""
from sqlalchemy import select, text
from app.models.folder import Folder

TREE_LOCK_NAMESPACE = 1179601988  # Separate from the organizer's execution lease.


def lock_tree(session, workspace_id: int):
    # Production uses PostgreSQL. The transaction releases this lock on commit,
    # rollback or connection loss, including process crashes.
    if session.get_bind().dialect.name == 'postgresql':
        session.execute(text('SELECT pg_advisory_xact_lock(:namespace, :workspace)'),
                        {'namespace': TREE_LOCK_NAMESPACE, 'workspace': workspace_id})


def workspace_id_for_folder(session, folder_id: int):
    return session.scalar(select(Folder.workspace_id).where(Folder.id == folder_id)
                          .execution_options(autoflush=False))


def get_folder(session, folder_id: int):
    return session.get(Folder, folder_id, populate_existing=True)


def root_id(session, workspace_id: int):
    # Deterministic selection also permits recovery of historical extra roots.
    return session.scalar(select(Folder.id).where(Folder.workspace_id == workspace_id,
                          Folder.parent_id.is_(None)).order_by(Folder.id).limit(1))
