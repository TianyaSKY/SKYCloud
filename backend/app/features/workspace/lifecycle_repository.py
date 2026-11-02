"""在调用方事务中执行的工作空间删除 SQL 操作."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.workspace import Workspace


def lock_workspace(session: Session, workspace_id: int) -> Workspace | None:
    """为调用方的删除事务锁定工作空间行记录."""
    return session.scalar(
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True),
    )


def delete_workspace(session: Session, workspace_id: int) -> None:
    """在调用方事务中删除工作空间及其级联关联记录."""
    session.execute(
        delete(Workspace)
        .where(Workspace.id == workspace_id)
        .execution_options(synchronize_session="fetch"),
    )
