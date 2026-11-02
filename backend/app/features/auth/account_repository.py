"""使用调用方 Session 和事务的账户生命周期 SQL 操作."""

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.opencode_runtime import OpenCodeRuntime
from app.models.user import User
from app.models.workspace import Workspace


def lock_user(session: Session, user_id: int) -> User | None:
    """加锁并重新加载单条账户记录."""
    return session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True),
    )


def lock_owned_workspaces(session: Session, user_id: int) -> list[Workspace]:
    """按主键顺序加锁该账户拥有的所有工作区."""
    return list(
        session.scalars(
            select(Workspace)
            .where(Workspace.owner_id == user_id)
            .order_by(Workspace.id)
            .with_for_update(),
        ),
    )


def user_runtimes(session: Session, user_id: int) -> list[OpenCodeRuntime]:
    """按主键顺序列出该账户拥有的所有运行时."""
    return list(
        session.scalars(
            select(OpenCodeRuntime)
            .where(OpenCodeRuntime.user_id == user_id)
            .order_by(OpenCodeRuntime.id),
        ),
    )


def delete_account(session: Session, user_id: int) -> None:
    """删除账户行记录; 由数据库级联删除完成关联清理."""
    session.execute(
        delete(User)
        .where(User.id == user_id)
        .execution_options(synchronize_session="fetch"),
    )


def current_role(session: Session, user_id: int) -> str | None:
    """直接读取当前 role 列, 绕过缓存的 profile/ORM 身份状态."""
    return session.scalar(select(User.role).where(User.id == user_id))


def set_avatar(session: Session, user_id: int, avatar_url: str) -> bool:
    """在调用方的发布事务中暂存账户头像元数据."""
    result = session.execute(
        update(User).where(User.id == user_id).values(avatar=avatar_url),
    )
    if isinstance(result, CursorResult):
        return result.rowcount == 1
    return False


def lock_users(session: Session, user_ids: set[int]) -> list[User]:
    """按确定顺序锁定变更涉及的实体记录并刷新其状态."""
    return list(
        session.scalars(
            select(User)
            .where(User.id.in_(user_ids))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True),
        ),
    )
