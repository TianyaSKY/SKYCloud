"""在 API/Worker 进程中断后恢复已准入的运行时 generation."""

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
    """记录单个中断的 generation 以便后续尝试恢复."""
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
    """重启准入依然有效的被中断 generation."""
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
                # 按 generation 尽力隔离: Docker 生命周期与
                # 其 SQL 记账操作各自抛出不同的异常家族。
                session.rollback()
                defer_operation(session, generation, error)
    return recovered
