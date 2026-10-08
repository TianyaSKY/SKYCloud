"""Current workspace identity locks in account/lifecycle deletion order."""
from sqlalchemy import select
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


def lock_write_identity(session, workspace_id: int, user_id: int):
    def lock(model, *conditions):
        return session.scalar(select(model).where(*conditions).with_for_update()
                              .execution_options(populate_existing=True))
    user = lock(User, User.id == user_id)
    if user is None:
        return None
    workspace = lock(Workspace, Workspace.id == workspace_id)
    if workspace is None:
        return None
    return lock(WorkspaceMember, WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id)
