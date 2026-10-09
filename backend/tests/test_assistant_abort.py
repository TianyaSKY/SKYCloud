from datetime import timedelta
from types import SimpleNamespace
import time
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now
from app.infra import assistant_abort as worker
from app.models.assistant_runtime_abort import AssistantRuntimeAbort
from app.models.assistant import AssistantRun
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.opencode_runtime import OpenCodeRuntime
from app.features.assistant import repository, service, abort_repository
from app.exceptions import ConflictError
from runtime_controller.client import ControllerNotFound

@pytest.fixture
def seeded():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        user = User(username='abort-owner', password_hash='unused', role='common')
        db.add(user); db.flush()
        workspace = Workspace(name='abort-workspace', owner_id=user.id)
        db.add(workspace); db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin'))
        runtime = OpenCodeRuntime(workspace_id=workspace.id, user_id=user.id, container_id='a'*64, status='running')
        db.add(runtime); db.flush()
        conversation = repository.create_conversation(db, workspace.id, user.id, 'expert', 'abort-test')
        conversation.opencode_runtime_id = runtime.id
        request = repository.create_message(db, conversation, 'user', 'query')
        response = repository.create_message(db, conversation, 'assistant', '', status='streaming')
        run = repository.create_run(db, conversation, request, response)
        run.status = 'running'
        run.opencode_session_id = 'captured-session'
        run.runtime_lease_runtime_id = runtime.id
        run.runtime_lease_container_id = 'a'*64
        run.started_at = utc_now() - timedelta(seconds=60)
        run.metadata_dict = {'worker_heartbeat_unix': time.time()-60}
        db.commit()
        identity = user.id, workspace.id, runtime.id, conversation.id, run.id
    yield factory, identity
    engine.dispose()

def controller(identity, *, change=None):
    user, workspace, runtime, _, _ = identity
    labels = {'skycloud.workspace_id': str(workspace), 'skycloud.user_id': str(user), 'skycloud.runtime_id': str(runtime)}
    if change == 'identity':
        labels['skycloud.user_id'] = '999'
    container = SimpleNamespace(id='a'*64, status='exited' if change == 'stopped' else 'running', attrs={'Config': {'Labels': labels}})
    def get(ref):
        assert ref == 'a'*64
        if change == 'gone':
            raise ControllerNotFound('Absent')
        return container
    return lambda: SimpleNamespace(containers=SimpleNamespace(get=get))

def test_recovery_intent_is_atomic_and_retry_blocks_new_admission(seeded):
    factory, identity = seeded
    user, workspace, runtime, conversation, run_id = identity
    with factory() as db:
        assert service._recover_abandoned_run(db, db.get(AssistantRun, run_id), commit=False)
        assert abort_repository.pending_for_member(db, workspace, user)
        db.rollback()
    with factory() as db:
        assert db.get(AssistantRuntimeAbort, run_id) is None
        assert db.get(AssistantRun, run_id).status == 'running'
        assert service._recover_abandoned_run(db, db.get(AssistantRun, run_id))
    def unavailable(target):
        raise ConnectionError('private-controller-url')
    assert worker.abort_pending_sessions(session_factory=factory, controller_factory=controller(identity), abort_function=unavailable) == 0
    with factory() as db:
        intent = db.get(AssistantRuntimeAbort, run_id)
        assert intent.attempts == 1 and intent.last_error_type == 'ConnectionError' and intent.completed_at is None
        assert intent.next_attempt_at > utc_now()
        with pytest.raises(ConflictError, match='cleanup'):
            service.prepare_run(db, db.get(Workspace, workspace), user, conversation, 'new work')
        db.rollback()
        intent.next_attempt_at = utc_now()-timedelta(seconds=1)
        db.get(OpenCodeRuntime, runtime).container_id = 'b'*64
        db.commit()
    targets = []
    assert worker.abort_pending_sessions(session_factory=factory, controller_factory=controller(identity), abort_function=targets.append) == 1
    assert len(targets) == 1 and targets[0].container_id == 'a'*64 and targets[0].session_id == 'captured-session'
    assert worker.abort_pending_sessions(session_factory=factory, controller_factory=controller(identity), abort_function=targets.append) == 0
    with factory() as db:
        assert not abort_repository.pending_for_member(db, workspace, user)
        assert db.get(AssistantRuntimeAbort, run_id).completed_at is not None

@pytest.mark.parametrize('change', ['identity', 'gone', 'stopped'])
def test_cleanup_validates_captured_identity_or_proves_container_not_running(seeded, change):
    factory, identity = seeded
    with factory() as db:
        assert service._recover_abandoned_run(db, db.get(AssistantRun, identity[-1]))
    calls = []
    completed = worker.abort_pending_sessions(session_factory=factory, controller_factory=controller(identity, change=change), abort_function=calls.append)
    assert not calls
    assert completed == (0 if change == 'identity' else 1)
    with factory() as db:
        assert (db.get(AssistantRuntimeAbort, identity[-1]).completed_at is None) == (change == 'identity')

def test_scheduler_sweep_recovers_abandoned_run_without_browser_poll(seeded):
    factory, identity = seeded
    assert worker.recover_abandoned_runs(session_factory=factory) == 1
    with factory() as db:
        assert db.get(AssistantRun, identity[-1]).status == 'failed'
        assert db.get(AssistantRuntimeAbort, identity[-1]) is not None
    assert worker.recover_abandoned_runs(session_factory=factory) == 0


def test_recovery_uses_original_dispatch_container_after_sql_pointer_changes(seeded):
    factory, identity = seeded
    with factory() as db:
        db.get(OpenCodeRuntime, identity[2]).container_id = 'b'*64
        db.commit()
        assert service._recover_abandoned_run(db, db.get(AssistantRun, identity[-1]))
        assert db.get(AssistantRuntimeAbort, identity[-1]).container_id == 'a'*64


def test_lease_without_dispatched_provider_session_still_has_cleanup_intent(seeded):
    factory, identity = seeded
    with factory() as db:
        run = db.get(AssistantRun, identity[-1])
        run.opencode_session_id = None
        run.runtime_lease_token = 'c'*32
        db.commit()
        assert 'runtime_lease_token' not in run.to_dict()
        assert service._recover_abandoned_run(db, run)
        intent = db.get(AssistantRuntimeAbort, run.id)
        assert intent.session_id is None and intent.lease_token == 'c'*32


def test_sweep_does_not_starve_dead_run_behind_older_live_run(seeded):
    factory, identity = seeded
    with factory() as db:
        old = db.get(AssistantRun, identity[-1])
        old.metadata_dict = {'worker_heartbeat_unix': time.time()}
        conversation = db.get(type(old.conversation), identity[3])
        request = repository.create_message(db, conversation, 'user', 'second')
        response = repository.create_message(db, conversation, 'assistant', '', status='streaming')
        old.status = 'failed'
        db.flush()
        new = repository.create_run(db, conversation, request, response)
        new.status = 'running'
        new.started_at = utc_now()-timedelta(seconds=60)
        new.metadata_dict = {'worker_heartbeat_unix': time.time()-60}
        db.commit()
        # SQL uniqueness permits one active run per conversation; give the
        # live predecessor its own conversation for this scan ordering proof.
        other = repository.create_conversation(db, identity[1], identity[0], 'fast', 'live')
        old.conversation_id = other.id
        old.status = 'running'
        db.commit()
        newer_id = new.id
    assert worker.recover_abandoned_runs(session_factory=factory, limit=1) == 1
    with factory() as db:
        assert db.get(AssistantRun, identity[-1]).status == 'running'
        assert db.get(AssistantRun, newer_id).status == 'failed'


@pytest.mark.parametrize('state', ['failed', 'cancel-requested'])
def test_expert_lease_checkpoint_refuses_ended_or_cancelled_run(seeded, state):
    from app.features.assistant.engines.expert_engine import ExpertEngine
    from app.models.assistant import AssistantConversation
    factory, identity = seeded
    with factory() as db:
        run = db.get(AssistantRun, identity[-1])
        if state == 'failed':
            run.status = 'failed'
        else:
            run.cancel_requested = True
        db.commit()
        engine = ExpertEngine(session=db, conversation=db.get(AssistantConversation, identity[3]), run=run)
    with pytest.raises(ConflictError):
        engine._save_runtime_lease(identity[1], identity[0], 'c'*32, identity[2], 'a'*64)
    with factory() as db:
        assert db.get(AssistantRun, identity[-1]).runtime_lease_token is None


@pytest.mark.parametrize('state', ['completed', 'failed', 'cancelled', 'cancel-requested'])
@pytest.mark.parametrize('operation', ['load-context', 'save-session'])
def test_paused_expert_cannot_resume_provider_work_after_run_ends(seeded, state, operation):
    from app.features.assistant.engines.expert_engine import ExpertEngine
    from app.models.assistant import AssistantConversation
    factory, identity = seeded
    with factory() as db:
        engine = ExpertEngine(session=db, conversation=db.get(AssistantConversation, identity[3]),
                              run=db.get(AssistantRun, identity[-1]))
    # The engine predates the committed state change; its next SQL operation
    # must validate the current row rather than trusting construction state.
    with factory() as db:
        run = db.get(AssistantRun, identity[-1])
        if state == 'cancel-requested':
            run.cancel_requested = True
        else:
            run.status = state
        db.commit()
    with pytest.raises(ConflictError):
        if operation == 'load-context':
            engine._load_context(identity[1], identity[0])
        else:
            engine._save_provider_session(identity[1], identity[0], identity[2], 'late-session')
    with factory() as db:
        conversation = db.get(AssistantConversation, identity[3])
        assert conversation.opencode_session_id is None
        assert db.get(AssistantRun, identity[-1]).opencode_session_id == 'captured-session'


def test_explicit_cancel_atomically_stages_remote_cleanup_and_blocks_new_run(seeded):
    factory, identity = seeded
    with factory() as db:
        service.request_cancel(db, db.get(Workspace, identity[1]), identity[0], identity[-1])
    with factory() as db:
        assert db.get(AssistantRun, identity[-1]).cancel_requested
        intent = db.get(AssistantRuntimeAbort, identity[-1])
        assert intent is not None and intent.session_id == 'captured-session'
        db.get(AssistantRun, identity[-1]).status = 'cancelled'
        db.commit()
        with pytest.raises(ConflictError, match='cleanup'):
            service.prepare_run(db, db.get(Workspace, identity[1]), identity[0], identity[3], 'new work')


@pytest.mark.parametrize('status', ['cancelled', 'failed'])
def test_interrupted_stream_finish_atomically_stages_remote_cleanup(seeded, status):
    from app.features.assistant.stream_persistence import StreamPersistence
    from app.models.assistant import AssistantConversation
    factory, identity = seeded
    with factory() as db:
        persistence = StreamPersistence(db, db.get(Workspace, identity[1]), identity[0],
            db.get(AssistantConversation, identity[3]), db.get(AssistantRun, identity[-1]))
        assert persistence.finish(status, '', {}, {}, [], None) == status
    with factory() as db:
        assert db.get(AssistantRun, identity[-1]).status == status
        intent = db.get(AssistantRuntimeAbort, identity[-1])
        assert intent is not None and intent.session_id == 'captured-session'
