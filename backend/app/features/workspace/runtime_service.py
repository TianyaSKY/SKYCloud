"""Persistence helpers for per-user OpenCode runtimes."""

from sqlalchemy.orm import Session

from app.models.opencode_runtime import OpenCodeRuntime


def get_runtime(session: Session, workspace_id: int, user_id: int) -> OpenCodeRuntime | None:
    """Return the runtime owned by one workspace member, if it exists."""

    return (
        session.query(OpenCodeRuntime)
        .filter_by(workspace_id=workspace_id, user_id=user_id)
        .first()
    )


def get_or_create_runtime(
    session: Session, workspace_id: int, user_id: int
) -> OpenCodeRuntime:
    """Get the unique runtime for a member, creating it without a container."""

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
    return (
        session.query(OpenCodeRuntime)
        .filter_by(workspace_id=workspace_id)
        .order_by(OpenCodeRuntime.user_id.asc())
        .all()
    )
