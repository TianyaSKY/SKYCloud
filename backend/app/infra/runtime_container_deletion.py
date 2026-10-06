"""Retry committed container cleanup without broad Docker access."""
from datetime import timedelta
from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy import func, select

from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionLocal
from app.models.runtime_container_deletion import RuntimeContainerDeletion
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation
from app.models.workspace import Workspace
from runtime_controller.client import ControllerClient, ControllerNotFound, ControllerError


def controller_client():
    return ControllerClient()


def stage_container_deletion_fallback(session: Session, container_id: str | None) -> None:
    # PostgreSQL UPDATE/DELETE triggers cover ORM, bulk and cascade paths.
    if container_id and session.get_bind().dialect.name != 'postgresql':
        session.add(RuntimeContainerDeletion(container_id=container_id))


def _clock(session: Session):
    # Queue defaults and Worker processes may have different wall clocks.
    return session.scalar(select(func.clock_timestamp())) if session.get_bind().dialect.name == 'postgresql' else utc_now()


def delete_pending_containers(*, session_factory=SessionLocal, controller_factory=None, limit: int = 20) -> int:
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            now = _clock(session)
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
                    client = (controller_factory or controller_client)()
                    container = None
                    try:
                        container = client.containers.get(intent.container_id)
                    except ControllerNotFound:
                        pass  # Absence alone does not retire a known generation.
                    except ControllerError:
                        # This endpoint accepts only exact, known historical
                        # names/labels. Auth and ownership refusals still fail.
                        client.remove_legacy(intent.container_id)
                    if container is not None and container.id != intent.container_id:
                        raise ControllerError('Container cleanup requires exact identity')
                    generations = session.query(RuntimeOperation).filter(RuntimeOperation.candidate_container_id == intent.container_id).all()
                    if generations:
                        for generation in generations:
                            if container is not None:
                                labels = container.attrs.get('Config',{}).get('Labels',{})
                                expected = {'skycloud.generation':generation.generation,
                                    'skycloud.workspace_id':str(generation.workspace_id),
                                    'skycloud.user_id':str(generation.user_id),'skycloud.runtime_id':str(generation.runtime_id)}
                                if any(labels.get(key) != value for key,value in expected.items()):
                                    raise ControllerError('Container generation identity mismatch')
                            client.retire_generation(generation.workspace_id,generation.user_id,generation.runtime_id,generation.generation)
                    elif container is not None:
                        try:
                            container.remove(force=True,v=True)
                        except ControllerNotFound:
                            pass  # Completed-marker replay / concurrent removal.
            except Exception as error:
                intent.last_error_type = type(error).__name__[:96]
                intent.next_attempt_at = _clock(session) + timedelta(seconds=min(300, 2 ** min(intent.attempts, 9)))
                session.commit()
                logger.warning('Container cleanup {} deferred ({})', intent.id, intent.last_error_type)
            else:
                intent.completed_at = _clock(session)
                intent.last_error_type = None
                session.commit()
                completed += 1
    return completed
