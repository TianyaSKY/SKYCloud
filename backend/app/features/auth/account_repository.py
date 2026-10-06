"""Account lifecycle SQL using the caller's Session and transaction."""
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.workspace import Workspace
from app.models.opencode_runtime import OpenCodeRuntime


def lock_user(session: Session, user_id: int) -> User | None:
    return session.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))


def lock_owned_workspaces(session: Session, user_id: int) -> list[Workspace]:
    return list(session.scalars(select(Workspace).where(Workspace.owner_id == user_id).order_by(Workspace.id).with_for_update()))


def user_runtimes(session: Session, user_id: int) -> list[OpenCodeRuntime]:
    return list(session.scalars(select(OpenCodeRuntime).where(OpenCodeRuntime.user_id == user_id).order_by(OpenCodeRuntime.id)))


def delete_account(session: Session, user_id: int) -> None:
    session.execute(delete(User).where(User.id == user_id).execution_options(synchronize_session='fetch'))
