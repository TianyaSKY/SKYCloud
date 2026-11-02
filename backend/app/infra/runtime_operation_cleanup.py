"""退役已提交的已取消 generation, 保留失败记录以待重试."""

from collections.abc import Callable
from datetime import timedelta

from loguru import logger
from runtime_controller.client import ControllerClient

from app.features.workspace.runtime_port import RuntimeController
from app.infra.extensions import SessionFactory, SessionLocal
from app.infra.runtime_container_deletion import clock
from app.models.runtime_operation import RuntimeOperation


def retire_cancelled_operations(
    *,
    session_factory: SessionFactory = SessionLocal,
    controller_factory: Callable[[], RuntimeController] = ControllerClient,
    limit: int = 20,
) -> int:
    """退役 Controller 仍持有的每个已取消 generation."""
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            operation = (
                session.query(RuntimeOperation)
                .filter(
                    RuntimeOperation.state == "cancelled",
                    RuntimeOperation.finished_at.is_(None),
                    RuntimeOperation.next_attempt_at <= clock(session),
                )
                .order_by(
                    RuntimeOperation.next_attempt_at,
                    RuntimeOperation.generation,
                )
                .with_for_update(skip_locked=True)
                .first()
            )
            if operation is None:
                break
            operation.attempts += 1
            try:
                # 绝不能将退役路由不支持导致的 404 错误视为成功。
                controller_factory().retire_generation(
                    operation.workspace_id,
                    operation.user_id,
                    operation.runtime_id,
                    operation.generation,
                )
            except Exception as error:
                # 尽最大努力退役: Controller 客户端拥有其自身的
                # 错误分类体系, 所有失败必须保持可重试状态。
                operation.last_error_type = type(error).__name__[:96]
                operation.next_attempt_at = clock(session) + timedelta(
                    seconds=min(300, 2 ** min(operation.attempts, 9)),
                )
                session.commit()
                logger.warning(
                    "Runtime generation retirement deferred ({})",
                    type(error).__name__,
                )
            else:
                operation.finished_at = clock(session)
                operation.last_error_type = None
                session.commit()
                completed += 1
    return completed
