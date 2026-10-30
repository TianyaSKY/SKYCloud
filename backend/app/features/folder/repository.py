"""Fresh folder reads and transaction-scoped serialization of tree mutations."""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.folder import Folder

TREE_LOCK_NAMESPACE = 1179601988  # Separate from the organizer's execution lease.


def lock_tree(session: Session, workspace_id: int) -> None:
    """Serialise tree mutations for one workspace until the transaction ends."""
    # Production uses PostgreSQL. The transaction releases this lock on commit,
    # rollback or connection loss, including process crashes.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :workspace)"),
            {"namespace": TREE_LOCK_NAMESPACE, "workspace": workspace_id},
        )


def workspace_id_for_folder(session: Session, folder_id: int) -> int | None:
    """Return the owning workspace of a folder without loading the row."""
    return session.scalar(
        select(Folder.workspace_id)
        .where(Folder.id == folder_id)
        .execution_options(autoflush=False),
    )


def get_folder(session: Session, folder_id: int) -> Folder | None:
    """Read a folder row, refreshing any identity-mapped instance."""
    return session.get(Folder, folder_id, populate_existing=True)


def lock_folder(session: Session, folder_id: int) -> Folder | None:
    """Load a folder row under a row-level lock for the current transaction."""
    return session.scalar(
        select(Folder)
        .where(Folder.id == folder_id)
        .with_for_update()
        .execution_options(populate_existing=True, autoflush=False),
    )


def root_id(session: Session, workspace_id: int) -> int | None:
    """Return the workspace root folder id."""
    # Deterministic selection also permits recovery of historical extra roots.
    return session.scalar(
        select(Folder.id)
        .where(Folder.workspace_id == workspace_id, Folder.parent_id.is_(None))
        .order_by(Folder.id)
        .limit(1),
    )
