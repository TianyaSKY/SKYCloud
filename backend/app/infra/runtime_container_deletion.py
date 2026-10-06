"""Retry committed container cleanup without broad Docker access."""
from datetime import timedelta
from loguru import logger
from sqlalchemy.orm import Session

from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionLocal
from app.models.runtime_container_deletion import RuntimeContainerDeletion
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.workspace import Workspace
from runtime_controller.client import ControllerClient, ControllerNotFound, ControllerError


def controller_client():
    return ControllerClient()


def stage_container_deletion_fallback(session: Session, container_id: str | None) -> None:
    # PostgreSQL UPDATE/DELETE triggers cover ORM, bulk and cascade paths.
    if container_id and session.get_bind().dialect.name != 'postgresql':
        session.add(RuntimeContainerDeletion(container_id=container_id))


def delete_pending_containers(*, session_factory=SessionLocal, controller_factory=None, limit: int = 20) -> int:
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            now = utc_now()
            intent = session.query(RuntimeContainerDeletion).filter(
                RuntimeContainerDeletion.completed_at.is_(None), RuntimeContainerDeletion.next_attempt_at <= now
            ).order_by(RuntimeContainerDeletion.id).with_for_update(skip_locked=True).first()
            if intent is None:
                break
            intent.attempts += 1
            try:
                # Never clean a container still assigned to a current record.
                runtime = session.query(OpenCodeRuntime.id).filter(OpenCodeRuntime.container_id == intent.container_id).first()
                workspace = session.query(Workspace.id).filter(Workspace.container_id == intent.container_id).first()
                if runtime is None and workspace is None:
                    try:
                        container = (controller_factory or controller_client)().containers.get(intent.container_id)
                        if container.id != intent.container_id:
                            raise ControllerError('Container cleanup requires exact identity')
                        container.remove(force=True, v=True)
                    except ControllerNotFound:
                        pass  # Already removed, including a completed-marker replay.
            except Exception as error:
                intent.last_error_type = type(error).__name__[:96]
                intent.next_attempt_at = now + timedelta(seconds=min(300, 2 ** min(intent.attempts, 9)))
                session.commit()
                logger.warning('Container cleanup {} deferred ({})', intent.id, intent.last_error_type)
            else:
                intent.completed_at = utc_now()
                intent.last_error_type = None
                session.commit()
                completed += 1
    return completed
