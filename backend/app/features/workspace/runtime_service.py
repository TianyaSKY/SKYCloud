"""单用户 OpenCode 运行时实体的持久化辅助函数."""

from sqlalchemy.orm import Session

from app.models.opencode_runtime import OpenCodeRuntime


def get_runtime(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> OpenCodeRuntime | None:
    """返回指定工作空间成员拥有的运行时实体 (若存在)."""
    return (
        session.query(OpenCodeRuntime)
        .filter_by(workspace_id=workspace_id, user_id=user_id)
        .first()
    )


def get_or_create_runtime(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> OpenCodeRuntime:
    """获取成员唯一的运行时记录, 若不存在则创建无容器的记录."""
    runtime = get_runtime(session, workspace_id, user_id)
    if runtime:
        return runtime

    runtime = OpenCodeRuntime(
        workspace_id=workspace_id,
        user_id=user_id,
        status="stopped",
        config_version=0,
    )
    session.add(runtime)
    session.flush()
    return runtime


def list_runtimes(session: Session, workspace_id: int) -> list[OpenCodeRuntime]:
    """按所有者排序返回工作空间下全部成员的运行时记录列表."""
    return (
        session.query(OpenCodeRuntime)
        .filter_by(workspace_id=workspace_id)
        .order_by(OpenCodeRuntime.user_id.asc())
        .all()
    )
