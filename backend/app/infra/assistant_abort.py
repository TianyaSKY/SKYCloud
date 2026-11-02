"""恢复遗留的 SQL 运行任务, 并重试捕获的远程会话清理."""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger
from runtime_common.contracts import ContainerPayload
from runtime_controller.client import (
    ControllerClient,
    ControllerError,
    ControllerNotFound,
    runtime_url,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.features.assistant import repository, service
from app.features.assistant.clients.opencode_auth import (
    server_password,
    server_username,
)
from app.features.assistant.clients.opencode_client import (
    OpenCodeActionError,
    OpenCodeClient,
)
from app.features.assistant.engines.runtime_lease import RELEASE
from app.infra import extensions
from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionFactory, SessionLocal
from app.models.assistant import AssistantRun
from app.models.assistant_runtime_abort import AssistantRuntimeAbort

CONTAINER_ID_PATTERN = r"[0-9a-f]{64}"
SETTLED_CONTAINER_STATES = frozenset({"exited", "dead", "created", "removing"})
ABANDONED_AFTER_SECONDS = 30


@dataclass(frozen=True)
class AbortTarget:
    """异常终止的 worker 所遗留的捕获远程身份标识."""

    workspace_id: int
    user_id: int
    runtime_id: int
    container_id: str
    generation: str | None
    session_id: str | None
    lease_token: str | None


def _clock(session: Session) -> datetime:
    """返回队列时钟基准, 确保默认值与 Worker 保持时间一致."""
    if session.get_bind().dialect.name == "postgresql":
        # clock_timestamp() 始终返回恰好一行非空记录。
        return session.execute(select(func.clock_timestamp())).scalar_one()
    return utc_now()


def abort_remote(target: AbortTarget) -> None:
    """通过运行时网关中止单个捕获的供应商会话."""
    session_id = target.session_id
    if session_id is None:
        # 仅记录了供应商会话信息的清理意图才会进入此处理路径。
        return

    async def abort() -> None:
        async with OpenCodeClient(
            runtime_url(target.container_id),
            server_username(),
            server_password(target.runtime_id, target.user_id, target.workspace_id),
            timeout=5,
        ) as client:
            if not await client.abort_session(session_id):
                message = "Remote session abort was not acknowledged"
                raise OpenCodeActionError(message)

    asyncio.run(abort())


def _exact_container_id(container_id: str) -> None:
    """拒绝清理操作, 除非捕获的容器身份标识完全精确."""
    if not re.fullmatch(CONTAINER_ID_PATTERN, container_id):
        message = "Remote cleanup requires exact container identity"
        raise ControllerError(message)


def _expected_labels(target: AbortTarget) -> dict[str, str]:
    """返回证明容器归属于该清理意图的标签字典."""
    expected = {
        "skycloud.workspace_id": str(target.workspace_id),
        "skycloud.user_id": str(target.user_id),
        "skycloud.runtime_id": str(target.runtime_id),
    }
    if target.generation:
        expected["skycloud.generation"] = target.generation
    return expected


def _verify_target(target: AbortTarget, container: ContainerPayload) -> None:
    """拒绝身份标识与捕获意图不一致的容器."""
    labels = container["labels"]
    expected = _expected_labels(target)
    if container["id"] != target.container_id or any(
        labels.get(key) != value for key, value in expected.items()
    ):
        message = "Remote cleanup identity mismatch"
        raise ControllerError(message)


def _abort_target(
    target: AbortTarget,
    controller_factory: Callable[[], ControllerClient],
    abort_function: Callable[[AbortTarget], None],
) -> None:
    """中止单个活跃的供应商会话, 随后释放其运行时租约."""
    container: ContainerPayload | None
    try:
        container = controller_factory().inspect(target.container_id)
    except ControllerNotFound:
        container = None
    if container is not None:
        _verify_target(target, container)
        if target.session_id and container["status"] not in SETTLED_CONTAINER_STATES:
            abort_function(target)
    if target.lease_token:
        extensions.redis_client.eval(
            RELEASE,
            1,
            f"assistant:expert:runtime:{target.runtime_id}",
            target.lease_token,
        )


def _defer_abort(
    session: Session,
    intent: AssistantRuntimeAbort,
    error: BaseException,
) -> None:
    """记录可重试的清理失败错误, 同时不泄露其异常详情明文."""
    intent.last_error_type = type(error).__name__[:96]
    intent.next_attempt_at = _clock(session) + timedelta(
        seconds=min(300, 2 ** min(intent.attempts, 9)),
    )
    session.commit()
    logger.warning(
        "Assistant remote cleanup deferred (error_type={})",
        type(error).__name__,
    )


def abort_pending_sessions(
    *,
    session_factory: SessionFactory = SessionLocal,
    controller_factory: Callable[[], ControllerClient] = ControllerClient,
    abort_function: Callable[[AbortTarget], None] = abort_remote,
    limit: int = 20,
) -> int:
    """每次锁定单个意图以重试捕获的远程会话清理."""
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            now = _clock(session)
            intent = (
                session.query(AssistantRuntimeAbort)
                .filter(
                    AssistantRuntimeAbort.completed_at.is_(None),
                    AssistantRuntimeAbort.next_attempt_at <= now,
                )
                .order_by(AssistantRuntimeAbort.run_id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if intent is None:
                break
            intent.attempts += 1
            target = AbortTarget(
                workspace_id=intent.workspace_id,
                user_id=intent.user_id,
                runtime_id=intent.runtime_id,
                container_id=intent.container_id,
                generation=intent.generation,
                session_id=intent.session_id,
                lease_token=intent.lease_token,
            )
            try:
                _exact_container_id(target.container_id)
                _abort_target(target, controller_factory, abort_function)
            except Exception as error:
                # 按意图尽最大努力隔离: Controller RPC 接口、
                # 供应商客户端与 Redis 各自抛出不同的异常家族。
                _defer_abort(session, intent, error)
            else:
                intent.completed_at = _clock(session)
                intent.last_error_type = None
                session.commit()
                completed += 1
    return completed


def recover_abandoned_runs(
    *,
    session_factory: SessionFactory = SessionLocal,
    limit: int = 100,
) -> int:
    """释放 SSE worker 异常消失后残留的活跃运行任务."""
    recovered = 0
    after = 0
    while recovered < limit:
        with session_factory() as selector:
            ids = (
                selector.query(AssistantRun.id)
                .filter(
                    AssistantRun.id > after,
                    AssistantRun.status.notin_(repository.TERMINAL_RUN_STATUSES),
                    func.coalesce(AssistantRun.started_at, AssistantRun.created_at)
                    <= _clock(selector)
                    - timedelta(seconds=ABANDONED_AFTER_SECONDS),
                )
                .order_by(AssistantRun.id)
                .limit(limit)
                .all()
            )
        if not ids:
            break
        for (run_id,) in ids:
            after = run_id
            with session_factory() as session:
                run = session.get(AssistantRun, run_id)
                if run is not None and service.recover_abandoned_run(session, run):
                    recovered += 1
            if recovered >= limit:
                break
    return recovered
