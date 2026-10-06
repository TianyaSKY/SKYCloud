"""Recover admitted runtime generations after API/Worker process interruption."""
from datetime import timedelta
from sqlalchemy import select
from app.infra.extensions import SessionLocal
from app.infra.runtime_container_deletion import _clock
from app.models.runtime_operation import RuntimeOperation
from app.models.workspace import Workspace


def defer_operation(session, generation, error):
    operation = session.scalar(select(RuntimeOperation).where(RuntimeOperation.generation == generation,
        RuntimeOperation.state.in_(['preparing','switched'])).with_for_update().execution_options(populate_existing=True))
    if operation is None:
        return
    operation.attempts += 1
    operation.last_error_type = type(error).__name__[:96]
    operation.next_attempt_at = _clock(session) + timedelta(seconds=min(300,2**min(operation.attempts,9)))
    session.commit()


def recover_runtime_operations(*, session_factory=SessionLocal, limit=20):
    from app.features.workspace.docker_service import start
    with session_factory() as session:
        rows = session.execute(select(RuntimeOperation.generation,RuntimeOperation.workspace_id,RuntimeOperation.user_id)
            .where(RuntimeOperation.state.in_(['preparing','switched']),RuntimeOperation.next_attempt_at <= _clock(session))
            .order_by(RuntimeOperation.next_attempt_at,RuntimeOperation.generation).limit(limit)).all()
    recovered = 0
    for generation,workspace_id,user_id in rows:
        with session_factory() as session:
            try:
                workspace = session.get(Workspace,workspace_id)
                if workspace is None:
                    continue
                start(session,workspace,user_id,expected_generation=generation)
                session.expire_all()
                operation = session.get(RuntimeOperation,generation)
                if operation.state == 'complete': recovered += 1
            except Exception as error:
                session.rollback()
                defer_operation(session,generation,error)
    return recovered
