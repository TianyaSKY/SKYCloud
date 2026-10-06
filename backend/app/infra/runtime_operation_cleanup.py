"""Retire committed cancelled generations, retaining failures for replay."""
from datetime import timedelta
from sqlalchemy.orm import Session
from loguru import logger

from app.infra.extensions import SessionLocal
from app.infra.runtime_container_deletion import _clock
from app.models.runtime_operation import RuntimeOperation
from runtime_controller.client import ControllerClient


def retire_cancelled_operations(*, session_factory=SessionLocal, controller_factory=ControllerClient, limit=20):
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            operation = session.query(RuntimeOperation).filter(
                RuntimeOperation.state == 'cancelled', RuntimeOperation.finished_at.is_(None),
                RuntimeOperation.next_attempt_at <= _clock(session)
            ).order_by(RuntimeOperation.next_attempt_at, RuntimeOperation.generation).with_for_update(skip_locked=True).first()
            if operation is None:
                break
            operation.attempts += 1
            try:
                # Never turn an unsupported retirement-route 404 into success.
                controller_factory().retire_generation(operation.workspace_id, operation.user_id,
                                                       operation.runtime_id, operation.generation)
            except Exception as error:
                operation.last_error_type = type(error).__name__[:96]
                operation.next_attempt_at = _clock(session) + timedelta(seconds=min(300, 2 ** min(operation.attempts, 9)))
                session.commit()
                logger.warning('Runtime generation retirement deferred ({})', type(error).__name__)
            else:
                operation.finished_at = _clock(session)
                operation.last_error_type = None
                session.commit()
                completed += 1
    return completed
