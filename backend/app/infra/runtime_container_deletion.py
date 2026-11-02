"""无需宽泛 Docker 权限, 重试已提交的容器清理任务."""

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta

from loguru import logger
from runtime_common.contracts import ContainerPayload
from runtime_controller.client import (
    ControllerClient,
    ControllerError,
    ControllerNotFound,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionFactory, SessionLocal
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_container_deletion import RuntimeContainerDeletion
from app.models.runtime_operation import RuntimeOperation
from app.models.workspace import Workspace


def controller_client() -> ControllerClient:
    """返回绑定到配置端点的 Controller 客户端."""
    return ControllerClient()


def stage_container_deletion_fallback(
    session: Session,
    container_id: str | None,
) -> None:
    """为未配置 PostgreSQL 触发器的数据库暂存删除意图."""
    # PostgreSQL UPDATE/DELETE 触发器覆盖 ORM、批量与级联删除路径。
    if container_id and session.get_bind().dialect.name != "postgresql":
        session.add(RuntimeContainerDeletion(container_id=container_id))


def clock(session: Session) -> datetime:
    """返回队列时钟基准, 确保默认值与 Worker 保持时间一致."""
    # 队列默认值与 Worker 进程可能具有不同的墙上时钟。
    if session.get_bind().dialect.name == "postgresql":
        # clock_timestamp() 始终返回恰好一行非空记录。
        return session.execute(select(func.clock_timestamp())).scalar_one()
    return utc_now()


def _inspect_container(
    client: ControllerClient,
    container_id: str,
) -> ContainerPayload | None:
    """检查单个精确容器, 回退至遗留删除接口."""
    try:
        return client.inspect(container_id)
    except ControllerNotFound:
        # 仅容器不存在并不足以退役已知 generation。
        return None
    except ControllerError:
        # 该端点仅接受精确、已知的历史名称/标签。
        # 鉴权与所有权拒绝仍会抛出失败。
        client.remove_legacy(container_id)
        return None


def _verify_container_identity(
    container: ContainerPayload,
    container_id: str,
) -> None:
    """拒绝清理完整身份标识不匹配的容器."""
    if container["id"] != container_id:
        message = "Container cleanup requires exact identity"
        raise ControllerError(message)


def _verify_generation(
    container: ContainerPayload,
    generation: RuntimeOperation,
) -> None:
    """拒绝标签与记录的 generation 不匹配的容器."""
    labels = container["labels"]
    expected = {
        "skycloud.generation": generation.generation,
        "skycloud.workspace_id": str(generation.workspace_id),
        "skycloud.user_id": str(generation.user_id),
        "skycloud.runtime_id": str(generation.runtime_id),
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        message = "Container generation identity mismatch"
        raise ControllerError(message)


def _clean_container(
    session: Session,
    client: ControllerClient,
    container_id: str,
) -> None:
    """退役所有记录的 generation 或删除未记录的容器."""
    container = _inspect_container(client, container_id)
    if container is not None:
        _verify_container_identity(container, container_id)
    generations = (
        session.query(RuntimeOperation)
        .filter(RuntimeOperation.candidate_container_id == container_id)
        .all()
    )
    if generations:
        for generation in generations:
            if container is not None:
                _verify_generation(container, generation)
            client.retire_generation(
                generation.workspace_id,
                generation.user_id,
                generation.runtime_id,
                generation.generation,
            )
    elif container is not None:
        with suppress(ControllerNotFound):
            # 完成标记重放 / 并发删除场景。
            client.action(container_id, "remove")


def _is_still_referenced(session: Session, container_id: str) -> bool:
    """返回当前运行时或工作区是否仍然拥有该容器."""
    runtime = (
        session.query(OpenCodeRuntime.id)
        .filter(OpenCodeRuntime.container_id == container_id)
        .first()
    )
    workspace = (
        session.query(Workspace.id)
        .filter(Workspace.container_id == container_id)
        .first()
    )
    return runtime is not None or workspace is not None


def _defer_cleanup(
    session: Session,
    intent: RuntimeContainerDeletion,
    error: BaseException,
) -> None:
    """记录可重试的清理失败错误, 同时不泄露其异常详情明文."""
    intent.last_error_type = type(error).__name__[:96]
    intent.next_attempt_at = clock(session) + timedelta(
        seconds=min(300, 2 ** min(intent.attempts, 9)),
    )
    session.commit()
    logger.warning(
        "Container cleanup {} deferred ({})",
        intent.id,
        intent.last_error_type,
    )


def delete_pending_containers(
    *,
    session_factory: SessionFactory = SessionLocal,
    controller_factory: Callable[[], ControllerClient] | None = None,
    limit: int = 20,
) -> int:
    """每次锁定单个意图以重试已提交的容器清理任务."""
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            now = clock(session)
            intent = (
                session.query(RuntimeContainerDeletion)
                .filter(
                    RuntimeContainerDeletion.completed_at.is_(None),
                    RuntimeContainerDeletion.next_attempt_at <= now,
                )
                .order_by(RuntimeContainerDeletion.id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if intent is None:
                break
            intent.attempts += 1
            try:
                # 绝不清理仍分配给当前有效记录的容器。
                if not _is_still_referenced(session, intent.container_id):
                    client = (controller_factory or controller_client)()
                    _clean_container(session, client, intent.container_id)
            except Exception as error:
                # 按行尽最大努力隔离: Controller RPC 接口与
                # 数据行更新各自抛出不同的异常家族。
                _defer_cleanup(session, intent, error)
            else:
                intent.completed_at = clock(session)
                intent.last_error_type = None
                session.commit()
                completed += 1
    return completed
