"""Recover abandoned SQL runs and retry captured remote session cleanup."""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger
from runtime_controller.client import (
    ControllerClient,
    ControllerError,
    ControllerNotFound,
    RemoteContainer,
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
    """Captured remote identity a dead worker left behind."""

    workspace_id: int
    user_id: int
    runtime_id: int
    container_id: str
    generation: str | None
    session_id: str | None
    lease_token: str | None


def _clock(session: Session) -> datetime:
    """Return the queue clock so defaults and Workers agree on time."""
    if session.get_bind().dialect.name == "postgresql":
        # clock_timestamp() always yields exactly one non-null row.
        return session.execute(select(func.clock_timestamp())).scalar_one()
    return utc_now()


def abort_remote(target: AbortTarget) -> None:
    """Abort one captured provider session through the runtime gateway."""
    session_id = target.session_id
    if session_id is None:
        # Only an intent that recorded a provider session reaches this path.
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
    """Refuse cleanup unless the captured container identity is exact."""
    if not re.fullmatch(CONTAINER_ID_PATTERN, container_id):
        message = "Remote cleanup requires exact container identity"
        raise ControllerError(message)


def _expected_labels(target: AbortTarget) -> dict[str, str]:
    """Return the labels that prove a container belongs to the intent."""
    expected = {
        "skycloud.workspace_id": str(target.workspace_id),
        "skycloud.user_id": str(target.user_id),
        "skycloud.runtime_id": str(target.runtime_id),
    }
    if target.generation:
        expected["skycloud.generation"] = target.generation
    return expected


def _verify_target(target: AbortTarget, container: RemoteContainer) -> None:
    """Refuse a container whose identity differs from the captured intent."""
    labels = container.attrs["Config"]["Labels"] or {}
    expected = _expected_labels(target)
    if container.id != target.container_id or any(
        labels.get(key) != value for key, value in expected.items()
    ):
        message = "Remote cleanup identity mismatch"
        raise ControllerError(message)


def _abort_target(
    target: AbortTarget,
    controller_factory: Callable[[], ControllerClient],
    abort_function: Callable[[AbortTarget], None],
) -> None:
    """Abort one live provider session, then release its runtime lease."""
    container: RemoteContainer | None
    try:
        container = controller_factory().containers.get(target.container_id)
    except ControllerNotFound:
        container = None
    if container is not None:
        _verify_target(target, container)
        if target.session_id and container.status not in SETTLED_CONTAINER_STATES:
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
    """Record a retryable cleanup failure without exposing its message."""
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
    """Retry captured remote session cleanup one locked intent at a time."""
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
                # Best-effort per-intent isolation: the Controller RPC surface,
                # the provider client and Redis raise unrelated families.
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
    """Release runs left active after their SSE worker disappeared."""
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
