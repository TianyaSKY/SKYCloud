"""Current workspace identity locks in account/lifecycle deletion order."""

from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

_ModelT = TypeVar("_ModelT")


def _lock(
    session: Session,
    model: type[_ModelT],
    *conditions: ColumnElement[bool],
) -> _ModelT | None:
    return session.scalar(
        select(model)
        .where(*conditions)
        .with_for_update()
        .execution_options(populate_existing=True),
    )


def lock_write_identity(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> WorkspaceMember | None:
    """Lock the user, workspace and membership in deletion order."""
    user = _lock(session, User, User.id == user_id)
    if user is None:
        return None
    workspace = _lock(session, Workspace, Workspace.id == workspace_id)
    if workspace is None:
        return None
    return _lock(
        session,
        WorkspaceMember,
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user_id,
    )
