"""Workspace deletion SQL in the caller's transaction."""
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from app.models.workspace import Workspace


def lock_workspace(session: Session, workspace_id: int) -> Workspace | None:
    return session.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update().execution_options(populate_existing=True))


def delete_workspace(session: Session, workspace_id: int) -> None:
    session.execute(delete(Workspace).where(Workspace.id == workspace_id).execution_options(synchronize_session='fetch'))
