"""Recover abandoned SQL runs and retry captured remote session cleanup."""
import asyncio
import re
from dataclasses import dataclass
from datetime import timedelta

from loguru import logger
from sqlalchemy import func, select

from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionLocal
from app.infra import extensions
from app.models.assistant import AssistantRun
from app.models.assistant_runtime_abort import AssistantRuntimeAbort
from runtime_controller.client import ControllerClient, ControllerError, ControllerNotFound, runtime_url


@dataclass(frozen=True)
class AbortTarget:
    workspace_id: int
    user_id: int
    runtime_id: int
    container_id: str
    generation: str | None
    session_id: str | None
    lease_token: str | None


def _clock(session):
    return session.scalar(select(func.clock_timestamp())) if session.get_bind().dialect.name == 'postgresql' else utc_now()


def abort_remote(target):
    from app.features.assistant.clients.opencode_auth import server_password, server_username
    from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeActionError
    async def abort():
        async with OpenCodeClient(runtime_url(target.container_id), server_username(),
                                  server_password(target.runtime_id, target.user_id, target.workspace_id), timeout=5) as client:
            if not await client.abort_session(target.session_id):
                raise OpenCodeActionError('Remote session abort was not acknowledged')
    asyncio.run(abort())


def abort_pending_sessions(*, session_factory=SessionLocal, controller_factory=ControllerClient, abort_function=abort_remote, limit=20):
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            now = _clock(session)
            intent = session.query(AssistantRuntimeAbort).filter(
                AssistantRuntimeAbort.completed_at.is_(None), AssistantRuntimeAbort.next_attempt_at <= now,
            ).order_by(AssistantRuntimeAbort.run_id).with_for_update(skip_locked=True).first()
            if intent is None:
                break
            intent.attempts += 1
            target = AbortTarget(intent.workspace_id, intent.user_id, intent.runtime_id,
                                 intent.container_id, intent.generation, intent.session_id, intent.lease_token)
            try:
                if not re.fullmatch('[0-9a-f]{64}', target.container_id):
                    raise ControllerError('Remote cleanup requires exact container identity')
                try:
                    container = controller_factory().containers.get(target.container_id)
                except ControllerNotFound:
                    container = None
                if container is not None:
                    labels = container.attrs.get('Config', {}).get('Labels', {})
                    expected = {'skycloud.workspace_id': str(target.workspace_id),
                                'skycloud.user_id': str(target.user_id), 'skycloud.runtime_id': str(target.runtime_id)}
                    if target.generation:
                        expected['skycloud.generation'] = target.generation
                    if container.id != target.container_id or any(labels.get(key) != value for key, value in expected.items()):
                        raise ControllerError('Remote cleanup identity mismatch')
                    if target.session_id and container.status not in {'exited', 'dead', 'created', 'removing'}:
                        abort_function(target)
                if target.lease_token:
                    from app.features.assistant.engines.runtime_lease import _RELEASE
                    extensions.redis_client.eval(_RELEASE, 1, f'assistant:expert:runtime:{target.runtime_id}', target.lease_token)
            except Exception as exc:
                intent.last_error_type = type(exc).__name__[:96]
                intent.next_attempt_at = _clock(session) + timedelta(seconds=min(300, 2 ** min(intent.attempts, 9)))
                session.commit()
                logger.warning('Assistant remote cleanup deferred (error_type={})', type(exc).__name__)
            else:
                intent.completed_at = _clock(session)
                intent.last_error_type = None
                session.commit()
                completed += 1
    return completed


def recover_abandoned_runs(*, session_factory=SessionLocal, limit=100):
    from app.features.assistant import service, repository
    recovered = 0
    after = 0
    while recovered < limit:
        with session_factory() as selector:
            ids = selector.query(AssistantRun.id).filter(
                AssistantRun.id > after,
                AssistantRun.status.notin_(repository.TERMINAL_RUN_STATUSES),
                func.coalesce(AssistantRun.started_at, AssistantRun.created_at) <= _clock(selector) - timedelta(seconds=30),
            ).order_by(AssistantRun.id).limit(limit).all()
        if not ids:
            break
        for (run_id,) in ids:
            after = run_id
            with session_factory() as session:
                run = session.get(AssistantRun, run_id)
                if run is not None and service._recover_abandoned_run(session, run):
                    recovered += 1
            if recovered >= limit:
                break
    return recovered
