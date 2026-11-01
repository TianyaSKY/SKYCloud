"""Retry committed container cleanup without broad Docker access."""

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
    """Return a Controller client bound to the configured endpoint."""
    return ControllerClient()


def stage_container_deletion_fallback(
    session: Session,
    container_id: str | None,
) -> None:
    """Stage a deletion intent for databases without the PostgreSQL trigger."""
    # PostgreSQL UPDATE/DELETE triggers cover ORM, bulk and cascade paths.
    if container_id and session.get_bind().dialect.name != "postgresql":
        session.add(RuntimeContainerDeletion(container_id=container_id))


def clock(session: Session) -> datetime:
    """Return the queue clock so defaults and Workers agree on time."""
    # Queue defaults and Worker processes may have different wall clocks.
    if session.get_bind().dialect.name == "postgresql":
        # clock_timestamp() always yields exactly one non-null row.
        return session.execute(select(func.clock_timestamp())).scalar_one()
    return utc_now()


def _inspect_container(
    client: ControllerClient,
    container_id: str,
) -> ContainerPayload | None:
    """Inspect one exact container, falling back to legacy removal."""
    try:
        return client.inspect(container_id)
    except ControllerNotFound:
        # Absence alone does not retire a known generation.
        return None
    except ControllerError:
        # This endpoint accepts only exact, known historical names/labels.
        # Auth and ownership refusals still fail.
        client.remove_legacy(container_id)
        return None


def _verify_container_identity(
    container: ContainerPayload,
    container_id: str,
) -> None:
    """Refuse to clean a container whose full identity differs."""
    if container["id"] != container_id:
        message = "Container cleanup requires exact identity"
        raise ControllerError(message)


def _verify_generation(
    container: ContainerPayload,
    generation: RuntimeOperation,
) -> None:
    """Refuse a container whose labels do not match the recorded generation."""
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
    """Retire every recorded generation or remove the unrecorded container."""
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
            # Completed-marker replay / concurrent removal.
            client.action(container_id, "remove")


def _is_still_referenced(session: Session, container_id: str) -> bool:
    """Return whether a current runtime or workspace still owns the container."""
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
    """Record a retryable cleanup failure without exposing its message."""
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
    """Retry committed container cleanup one locked intent at a time."""
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
                # Never clean a container still assigned to a current record.
                if not _is_still_referenced(session, intent.container_id):
                    client = (controller_factory or controller_client)()
                    _clean_container(session, client, intent.container_id)
            except Exception as error:
                # Best-effort per-row isolation: the Controller RPC surface and
                # the row update raise unrelated exception families.
                _defer_cleanup(session, intent, error)
            else:
                intent.completed_at = clock(session)
                intent.last_error_type = None
                session.commit()
                completed += 1
    return completed
