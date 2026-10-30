"""Recover admitted runtime generations after API/Worker process interruption."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.workspace import docker_service
from app.infra.extensions import SessionFactory, SessionLocal
from app.infra.runtime_container_deletion import clock
from app.models.runtime_operation import RuntimeOperation
from app.models.workspace import Workspace


def defer_operation(
    session: Session,
    generation: str,
    error: BaseException,
) -> None:
    """Record one interrupted generation for a later recovery attempt."""
    statement = (
        select(RuntimeOperation)
        .where(
            RuntimeOperation.generation == generation,
            RuntimeOperation.state.in_(["preparing", "switched"]),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    operation = session.scalar(statement)
    if operation is None:
        return
    operation.attempts += 1
    operation.last_error_type = type(error).__name__[:96]
    operation.next_attempt_at = clock(session) + timedelta(
        seconds=min(300, 2 ** min(operation.attempts, 9)),
    )
    session.commit()


def recover_runtime_operations(
    *,
    session_factory: SessionFactory = SessionLocal,
    limit: int = 20,
) -> int:
    """Restart interrupted generations whose admission is still active."""
    with session_factory() as session:
        statement = (
            select(
                RuntimeOperation.generation,
                RuntimeOperation.workspace_id,
                RuntimeOperation.user_id,
            )
            .where(
                RuntimeOperation.state.in_(["preparing", "switched"]),
                RuntimeOperation.next_attempt_at <= clock(session),
            )
            .order_by(
                RuntimeOperation.next_attempt_at,
                RuntimeOperation.generation,
            )
            .limit(limit)
        )
        rows = session.execute(statement).all()
    recovered = 0
    for generation, workspace_id, user_id in rows:
        with session_factory() as session:
            try:
                workspace = session.get(Workspace, workspace_id)
                if workspace is None:
                    continue
                docker_service.start(
                    session,
                    workspace,
                    user_id,
                    expected_generation=generation,
                )
                session.expire_all()
                operation = session.get(RuntimeOperation, generation)
                if operation is not None and operation.state == "complete":
                    recovered += 1
            except Exception as error:
                # Best-effort per-generation isolation: the Docker lifecycle
                # and its SQL bookkeeping raise unrelated exception families.
                session.rollback()
                defer_operation(session, generation, error)
    return recovered
