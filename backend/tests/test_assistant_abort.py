from collections.abc import Callable, Generator
from datetime import timedelta
import time
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.exceptions import ConflictError
from app.features.assistant import abort_repository, repository, service
from app.features.assistant.engines.expert_engine import ExpertEngine
from app.features.assistant.stream_persistence import (
    FinalOutcome,
    LeasePoll,
    StreamPersistence,
    StreamProgress,
)
from app.infra import assistant_abort as worker
from app.infra.datetime_utils import utc_now
from app.infra.extensions import Base
from app.models import (
    AssistantConversation,
    AssistantMessage,
    AssistantRun,
    AssistantRuntimeAbort,
    OpenCodeRuntime,
    User,
    Workspace,
    WorkspaceMember,
)
from runtime_controller.client import ControllerNotFound


@pytest.fixture
def seeded() -> Generator[tuple[sessionmaker[Session], tuple[int, int, int, int, int]], None, None]:
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
        conversation = repository.create_conversation(
            db,
            repository.CreateConversationParams(
                workspace_id=workspace.id,
                user_id=user.id,
                mode='expert',
                title='abort-test',
            ),
        )
        conversation.opencode_runtime_id = runtime.id
        request = repository.create_message(
            db,
            conversation,
            repository.CreateMessageParams(role='user', content='query'),
        )
        response = repository.create_message(
            db,
            conversation,
            repository.CreateMessageParams(
                role='assistant',
                content='',
                status='streaming',
            ),
        )
        run = repository.create_run(db, conversation, request, response)
        run.status = 'running'
        run.opencode_session_id = 'captured-session'
        run.runtime_lease_runtime_id = runtime.id
        run.runtime_lease_container_id = 'a'*64
        run.started_at = utc_now() - timedelta(seconds=60)
        run.metadata_dict = {'worker_heartbeat_unix': time.time()-60}
        db.commit()
        identity = (user.id, workspace.id, runtime.id, conversation.id, run.id)
    yield factory, identity
    engine.dispose()


def controller(identity: tuple[int, int, int, int, int], *, change: str | None = None) -> Callable[[], Any]:
    user, workspace, runtime, _, _ = identity
    labels = {'skycloud.workspace_id': str(workspace), 'skycloud.user_id': str(user), 'skycloud.runtime_id': str(runtime)}
    if change == 'identity':
        labels['skycloud.user_id'] = '999'
    container = SimpleNamespace(
        id='a'*64,
        status='exited' if change == 'stopped' else 'running',
        attrs={'Config': {'Labels': labels}},
    )

    def inspect(ref: str) -> dict[str, Any]:
        assert ref == 'a'*64
        if change == 'gone':
            raise ControllerNotFound('Absent')
        return {
            'id': container.id,
            'status': container.status,
            'labels': container.attrs['Config']['Labels'],
        }

    return lambda: SimpleNamespace(inspect=inspect)


def test_recovery_intent_is_atomic_and_retry_blocks_new_admission(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]]) -> None:
    factory, identity = seeded
    user, workspace, runtime, conversation, run_id = identity
    with factory() as db:
        run_obj = db.get(AssistantRun, run_id)
        assert run_obj is not None
        assert service.recover_abandoned_run(db, run_obj, commit=False)
        assert abort_repository.pending_for_member(db, workspace, user)
        db.rollback()
    with factory() as db:
        assert db.get(AssistantRuntimeAbort, run_id) is None
        r_after = db.get(AssistantRun, run_id)
        assert r_after is not None
        assert r_after.status == 'running'
        assert service.recover_abandoned_run(db, r_after)
    def unavailable(target: Any) -> None:
        raise ConnectionError('private-controller-url')
    assert worker.abort_pending_sessions(session_factory=factory, controller_factory=controller(identity), abort_function=unavailable) == 0
    with factory() as db:
        intent = db.get(AssistantRuntimeAbort, run_id)
        assert intent is not None
        assert intent.attempts == 1 and intent.last_error_type == 'ConnectionError' and intent.completed_at is None
        assert intent.next_attempt_at is not None
        assert intent.next_attempt_at > utc_now()
        ws_obj = db.get(Workspace, workspace)
        assert ws_obj is not None
        with pytest.raises(ConflictError, match='cleanup'):
            service.prepare_run(
                db,
                service.PrepareRunParams(
                    workspace=ws_obj,
                    user_id=user,
                    conversation_id=conversation,
                    query='new work',
                ),
            )
        db.rollback()
        intent.next_attempt_at = utc_now()-timedelta(seconds=1)
        rt_obj = db.get(OpenCodeRuntime, runtime)
        assert rt_obj is not None
        rt_obj.container_id = 'b'*64
        db.commit()
    targets: list[Any] = []
    assert worker.abort_pending_sessions(session_factory=factory, controller_factory=controller(identity), abort_function=targets.append) == 1
    assert len(targets) == 1 and targets[0].container_id == 'a'*64 and targets[0].session_id == 'captured-session'
    assert worker.abort_pending_sessions(session_factory=factory, controller_factory=controller(identity), abort_function=targets.append) == 0
    with factory() as db:
        assert not abort_repository.pending_for_member(db, workspace, user)
        i_done = db.get(AssistantRuntimeAbort, run_id)
        assert i_done is not None
        assert i_done.completed_at is not None


@pytest.mark.parametrize('change', ['identity', 'gone', 'stopped'])
def test_cleanup_validates_captured_identity_or_proves_container_not_running(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]], change: str) -> None:
    factory, identity = seeded
    with factory() as db:
        r_obj = db.get(AssistantRun, identity[-1])
        assert r_obj is not None
        assert service.recover_abandoned_run(db, r_obj)
    calls: list[Any] = []
    completed = worker.abort_pending_sessions(session_factory=factory, controller_factory=controller(identity, change=change), abort_function=calls.append)
    assert not calls
    assert completed == (0 if change == 'identity' else 1)
    with factory() as db:
        i_obj = db.get(AssistantRuntimeAbort, identity[-1])
        assert i_obj is not None
        assert (i_obj.completed_at is None) == (change == 'identity')


def test_scheduler_sweep_recovers_abandoned_run_without_browser_poll(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]]) -> None:
    factory, identity = seeded
    assert worker.recover_abandoned_runs(session_factory=factory) == 1
    with factory() as db:
        r_obj = db.get(AssistantRun, identity[-1])
        assert r_obj is not None
        assert r_obj.status == 'failed'
        assert db.get(AssistantRuntimeAbort, identity[-1]) is not None
    assert worker.recover_abandoned_runs(session_factory=factory) == 0


def test_recovery_uses_original_dispatch_container_after_sql_pointer_changes(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]]) -> None:
    factory, identity = seeded
    with factory() as db:
        rt_obj = db.get(OpenCodeRuntime, identity[2])
        assert rt_obj is not None
        rt_obj.container_id = 'b'*64
        db.commit()
        r_obj = db.get(AssistantRun, identity[-1])
        assert r_obj is not None
        assert service.recover_abandoned_run(db, r_obj)
        i_obj = db.get(AssistantRuntimeAbort, identity[-1])
        assert i_obj is not None
        assert i_obj.container_id == 'a'*64


def test_lease_without_dispatched_provider_session_still_has_cleanup_intent(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]]) -> None:
    factory, identity = seeded
    with factory() as db:
        run = db.get(AssistantRun, identity[-1])
        assert run is not None
        run.opencode_session_id = None
        run.runtime_lease_token = 'c'*32
        db.commit()
        assert 'runtime_lease_token' not in run.to_dict()
        assert service.recover_abandoned_run(db, run)
        intent = db.get(AssistantRuntimeAbort, run.id)
        assert intent is not None
        assert intent.session_id is None and intent.lease_token == 'c'*32


def test_sweep_does_not_starve_dead_run_behind_older_live_run(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]]) -> None:
    factory, identity = seeded
    with factory() as db:
        old = db.get(AssistantRun, identity[-1])
        assert old is not None
        old.metadata_dict = {'worker_heartbeat_unix': time.time()}
        conversation = db.get(AssistantConversation, identity[3])
        assert conversation is not None
        request = repository.create_message(
            db,
            conversation,
            repository.CreateMessageParams(role='user', content='second'),
        )
        response = repository.create_message(
            db,
            conversation,
            repository.CreateMessageParams(
                role='assistant',
                content='',
                status='streaming',
            ),
        )
        old.status = 'failed'
        db.flush()
        new = repository.create_run(db, conversation, request, response)
        new.status = 'running'
        new.started_at = utc_now()-timedelta(seconds=60)
        new.metadata_dict = {'worker_heartbeat_unix': time.time()-60}
        db.commit()
        # SQL 唯一性约束保证每个对话只有一个活跃运行;
        # 为活跃前序分配其独立对话以证明扫描顺序.
        other = repository.create_conversation(
            db,
            repository.CreateConversationParams(
                workspace_id=identity[1],
                user_id=identity[0],
                mode='fast',
                title='live',
            ),
        )
        old.conversation_id = other.id
        old.status = 'running'
        db.commit()
        newer_id = new.id
    assert worker.recover_abandoned_runs(session_factory=factory, limit=1) == 1
    with factory() as db:
        r_old = db.get(AssistantRun, identity[-1])
        assert r_old is not None
        assert r_old.status == 'running'
        r_new = db.get(AssistantRun, newer_id)
        assert r_new is not None
        assert r_new.status == 'failed'


@pytest.mark.parametrize('state', ['failed', 'cancel-requested'])
def test_expert_lease_checkpoint_refuses_ended_or_cancelled_run(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]], state: str) -> None:
    factory, identity = seeded
    with factory() as db:
        run = db.get(AssistantRun, identity[-1])
        assert run is not None
        if state == 'failed':
            run.status = 'failed'
        else:
            run.cancel_requested = True
        db.commit()
        conv_obj = db.get(AssistantConversation, identity[3])
        assert conv_obj is not None
        engine = ExpertEngine(session=db, conversation=conv_obj, run=run)
    with pytest.raises(ConflictError):
        engine._save_runtime_lease(identity[1], identity[0], 'c'*32, identity[2], 'a'*64)  # pyright: ignore[reportPrivateUsage]
    with factory() as db:
        r_check = db.get(AssistantRun, identity[-1])
        assert r_check is not None
        assert r_check.runtime_lease_token is None


@pytest.mark.parametrize('state', ['completed', 'failed', 'cancelled', 'cancel-requested'])
@pytest.mark.parametrize('operation', ['load-context', 'save-session'])
def test_paused_expert_cannot_resume_provider_work_after_run_ends(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]], state: str, operation: str) -> None:
    factory, identity = seeded
    with factory() as db:
        conv_obj = db.get(AssistantConversation, identity[3])
        assert conv_obj is not None
        run_obj = db.get(AssistantRun, identity[-1])
        assert run_obj is not None
        engine = ExpertEngine(session=db, conversation=conv_obj, run=run_obj)
    # 引擎早于已提交的状态变更; 其下一次 SQL 操作
    # 必须校验当前行状态, 而不是信任构造时的状态.
    with factory() as db:
        run = db.get(AssistantRun, identity[-1])
        assert run is not None
        if state == 'cancel-requested':
            run.cancel_requested = True
        else:
            run.status = state
        db.commit()
    with pytest.raises(ConflictError):
        if operation == 'load-context':
            engine._load_context(identity[1], identity[0])  # pyright: ignore[reportPrivateUsage]
        else:
            engine._save_provider_session(identity[1], identity[0], identity[2], 'late-session')  # pyright: ignore[reportPrivateUsage]
    with factory() as db:
        conversation = db.get(AssistantConversation, identity[3])
        assert conversation is not None
        assert conversation.opencode_session_id is None
        r_final = db.get(AssistantRun, identity[-1])
        assert r_final is not None
        assert r_final.opencode_session_id == 'captured-session'


def test_explicit_cancel_atomically_stages_remote_cleanup_and_blocks_new_run(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]]) -> None:
    factory, identity = seeded
    with factory() as db:
        ws_obj = db.get(Workspace, identity[1])
        assert ws_obj is not None
        service.request_cancel(db, ws_obj, identity[0], identity[-1])
    with factory() as db:
        r_obj = db.get(AssistantRun, identity[-1])
        assert r_obj is not None
        assert r_obj.cancel_requested
        intent = db.get(AssistantRuntimeAbort, identity[-1])
        assert intent is not None and intent.session_id == 'captured-session'
        r_obj.status = 'cancelled'
        db.commit()
        ws_re = db.get(Workspace, identity[1])
        assert ws_re is not None
        with pytest.raises(ConflictError, match='cleanup'):
            service.prepare_run(
                db,
                service.PrepareRunParams(
                    workspace=ws_re,
                    user_id=identity[0],
                    conversation_id=identity[3],
                    query='new work',
                ),
            )


@pytest.mark.parametrize('status', ['cancelled', 'failed'])
def test_interrupted_stream_finish_atomically_stages_remote_cleanup(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]], status: str) -> None:
    factory, identity = seeded
    with factory() as db:
        ws_obj = db.get(Workspace, identity[1])
        assert ws_obj is not None
        conv_obj = db.get(AssistantConversation, identity[3])
        assert conv_obj is not None
        run_obj = db.get(AssistantRun, identity[-1])
        assert run_obj is not None
        persistence = StreamPersistence(db, ws_obj, identity[0], conv_obj, run_obj)
        assert persistence.finish(FinalOutcome(
            status=status,
            content='',
            metadata={},
            usage={},
            events=[],
            error_code=None,
        )) == status
    with factory() as db:
        r_obj = db.get(AssistantRun, identity[-1])
        assert r_obj is not None
        assert r_obj.status == status
        intent = db.get(AssistantRuntimeAbort, identity[-1])
        assert intent is not None and intent.session_id == 'captured-session'


def test_idle_stream_commits_partial_checkpoint_before_quiet_provider_wait(seeded: tuple[sessionmaker[Session], tuple[int, int, int, int, int]]) -> None:
    factory, identity = seeded
    with factory() as db:
        run = db.get(AssistantRun, identity[-1])
        assert run is not None
        message_id = run.assistant_message_id
        ws_obj = db.get(Workspace, identity[1])
        assert ws_obj is not None
        conv_obj = db.get(AssistantConversation, identity[3])
        assert conv_obj is not None
        persistence = StreamPersistence(db, ws_obj, identity[0], conv_obj, run)
        assert persistence.checkpoint(
            StreamProgress(
                answer_parts=['partial answer'],
                metadata={},
                usage={},
            ),
            commit=False,
        )
        assert persistence.poll(LeasePoll(
            renew_lease=False,
            now=time.time(),
            idle=True,
        )) == (False, False)
        db.rollback()
        msg_obj = db.get(AssistantMessage, message_id)
        assert msg_obj is not None
        assert msg_obj.content == 'partial answer'

