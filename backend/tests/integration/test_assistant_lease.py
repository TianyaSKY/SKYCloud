"""Isolated PostgreSQL checks for cross-worker execution heartbeat/recovery."""
import concurrent.futures
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
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


RAG_FAILURE = r'''
import asyncio
import json
import os
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import RunnableLambda
from sqlalchemy.orm import Session
from app.infra.extensions import engine
from app.features.assistant import repository, service
from app.features.chat import service as rag
from app.features.chat.query_rewrite import RewriteKeywordDimensions
from app.models.assistant import AssistantMessage
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

PRIVATE = 'private-rag-provider-diagnostic'
phase = os.environ['SKYCLOUD_RAG_FAILURE_PHASE']
closed = []

class FailingAnswer(FakeListChatModel):
    async def _astream(self, *args, **kwargs):
        try:
            yield ChatGenerationChunk(message=AIMessageChunk(content='partial-answer'))
            raise RuntimeError(PRIVATE)
        finally:
            closed.append('answer')

class Model:
    def with_structured_output(self, *args, **kwargs):
        def rewrite(prompt):
            if phase == 'rewrite':
                raise RuntimeError(PRIVATE)
            return RewriteKeywordDimensions(topic_terms=[], entity_terms=[], time_terms=[],
                file_type_terms=[], action_terms=[], synonym_terms=[])
        return RunnableLambda(rewrite)
    def with_config(self, config):
        return FailingAnswer(responses=['unused']).with_config(config)

class Embeddings:
    async def aembed_query(self, *args, **kwargs):
        return [.1] * 1024
    async def aembed_documents(self, texts, **kwargs):
        return [[.1] * 1024 for _ in texts]

rag.get_chat_model = lambda: Model()
rag.get_embeddings_model = lambda: Embeddings()
rag._db_search_by_vector = lambda *args: []

async def main():
    with Session(engine) as db:
        user = User(username='rag-failure-owner', password_hash='unused', role='common')
        db.add(user); db.flush()
        workspace = Workspace(name='rag-failure', owner_id=user.id)
        db.add(workspace); db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin'))
        conversation = repository.create_conversation(db, workspace.id, user.id, 'fast', 'failure')
        request = repository.create_message(db, conversation, 'user', 'query')
        answer = repository.create_message(db, conversation, 'assistant', '', status='streaming')
        run = repository.create_run(db, conversation, request, answer)
        db.commit()
        events = [event async for event in service.stream_prepared_run(
            db, workspace, user.id, conversation, run, [])]
        db.refresh(run)
        db.refresh(answer)
        assert run.status == 'failed', run.status
        assert answer.status == 'failed', answer.status
        assert events[-1].type == 'error' and not any(event.type == 'done' for event in events)
        assert PRIVATE not in json.dumps([event.to_dict() for event in events])
        assert PRIVATE not in json.dumps(run.to_dict())
        assert answer.content == ('partial-answer' if phase == 'answer' else '')
        if phase == 'answer':
            assert closed == ['answer']

asyncio.run(main())
print('REAL_RAG_FAILURE_PERSISTED')
'''


@pytest.mark.parametrize('phase', ['rewrite', 'answer'])
def test_rag_failures_persist_failed_runs_with_real_langchain(postgres, phase):
    upgrade(postgres)
    environment = {**os.environ, 'DATABASE_URL': postgres.url.render_as_string(hide_password=False),
                   'SKYCLOUD_RAG_FAILURE_PHASE': phase}
    result = subprocess.run([sys.executable, '-c', RAG_FAILURE],
        cwd=Path(__file__).resolve().parents[2], env=environment,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    assert 'REAL_RAG_FAILURE_PERSISTED' in result.stdout


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
