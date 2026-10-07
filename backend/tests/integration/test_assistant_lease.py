"""Isolated PostgreSQL checks for cross-worker execution heartbeat/recovery."""
import concurrent.futures
from datetime import timedelta
import os
import threading
import time

import pytest
from sqlalchemy.orm import Session
from entry.migrate import upgrade
from tests.integration.test_migrations import postgres
from app.features.assistant import repository, service
from app.infra.datetime_utils import beijing_now
from app.models.assistant import AssistantRun
from app.models.user import User
from app.models.workspace import Workspace

pytestmark = pytest.mark.skipif(os.getenv('RUN_MIGRATION_TESTS') != '1', reason='Opt-in isolated PostgreSQL lease checks')


def create_run(engine):
    upgrade(engine)
    with Session(engine) as session:
        user = User(username='lease-user', password_hash='unused-test-hash', role='common')
        session.add(user); session.flush()
        workspace = Workspace(name='lease-space', owner_id=user.id)
        session.add(workspace); session.flush()
        conversation = repository.create_conversation(session, workspace.id, user.id, 'expert', 'task')
        request = repository.create_message(session, conversation, 'user', 'patch')
        response = repository.create_message(session, conversation, 'assistant', '', status='streaming')
        run = repository.create_run(session, conversation, request, response)
        repository.set_pending_permission(run, {'permission_id': 'p1', 'title': 'Patch file'})
        run.started_at = beijing_now() - timedelta(minutes=5)
        session.commit()
        return run.id


def test_other_worker_preserves_live_permission_and_recovers_expired_lease(postgres):
    run_id = create_run(postgres)
    with Session(postgres) as worker, Session(postgres) as observer:
        old = repository.lock_run(observer, run_id); observer.commit()
        assert repository.touch_run_lease(worker, run_id, time.time())
        assert not service._recover_abandoned_run(observer, old)
        assert old.to_dict()['pending_permission']['permission_id'] == 'p1'
        observer.commit()
        assert repository.touch_run_lease(worker, run_id, time.time() - 60)
        assert service._recover_abandoned_run(observer, old)
        assert old.status == 'failed'; assert old.to_dict()['pending_permission'] is None
        assert not repository.touch_run_lease(worker, run_id, time.time())
        observer.refresh(old); assert old.status == 'failed'


def test_heartbeat_and_recovery_serialize_on_the_same_run_row(postgres):
    run_id = create_run(postgres)
    started = threading.Event()
    def recover():
        with Session(postgres) as observer:
            old = observer.get(AssistantRun, run_id)
            started.set()
            result = service._recover_abandoned_run(observer, old)
            observer.commit()
            return result
    with Session(postgres) as worker, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        row = repository.lock_run(worker, run_id)
        row.metadata_dict = {**row.metadata_dict, 'worker_heartbeat_unix': time.time()}
        worker.flush()
        pending = executor.submit(recover)
        assert started.wait(5)
        try:
            with pytest.raises(concurrent.futures.TimeoutError):
                pending.result(timeout=0.1)
        finally:
            worker.commit()
        assert pending.result(timeout=5) is False


@pytest.mark.parametrize('change',['demote','remove'])
def test_stream_poll_observes_committed_permission_loss_with_cached_member(postgres,change):
    from sqlalchemy import text
    from app.models.workspace import WorkspaceMember
    from app.models.assistant import AssistantConversation
    from app.features.assistant.stream_persistence import StreamPersistence
    from app.features.workspace.permissions import assert_can_write
    from app.exceptions import PermissionDeniedError
    run_id=create_run(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) SELECT workspace_id,user_id,'editor' FROM assistant_conversations LIMIT 1"))
    with Session(postgres) as worker:
        run=worker.get(AssistantRun,run_id)
        conversation=worker.get(AssistantConversation,run.conversation_id)
        workspace=worker.get(Workspace,conversation.workspace_id)
        cached=worker.query(WorkspaceMember).one()
        persistence=StreamPersistence(worker,workspace,conversation.user_id,conversation,run)
        assert persistence.poll(False,time.time()) == (False,False)
        with postgres.begin() as db:
            db.execute(text("UPDATE workspace_members SET role='viewer'" if change=='demote' else 'DELETE FROM workspace_members'))
        assert cached.role=='editor'
        assert persistence.poll(False,time.time()) == (False,True)
        with pytest.raises(PermissionDeniedError): assert_can_write(worker,workspace.id,conversation.user_id)
