"""Workspace deletion SQL in the caller's transaction."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.workspace import Workspace


def lock_workspace(session: Session, workspace_id: int) -> Workspace | None:
    """Lock the workspace row for the caller's deletion transaction."""
    return session.scalar(
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True),
    )


def delete_workspace(session: Session, workspace_id: int) -> None:
    """Delete the workspace and its cascading rows in the caller's transaction."""
    session.execute(
        delete(Workspace)
        .where(Workspace.id == workspace_id)
        .execution_options(synchronize_session="fetch"),
    )
