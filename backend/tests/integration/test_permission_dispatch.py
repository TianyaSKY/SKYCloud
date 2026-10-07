"""Real SQL ordering; provider acknowledgment is an explicit test seam."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import os
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.exceptions import PermissionDeniedError, ConflictError
from app.features.assistant.engines import expert_engine as module
from app.models.assistant import AssistantRun, AssistantConversation
from app.models.workspace import Workspace, WorkspaceMember
from app.models.opencode_runtime import OpenCodeRuntime
from tests.integration.test_migrations import postgres
from tests.integration.test_assistant_lease import create_run

pytestmark=pytest.mark.skipif(os.getenv('RUN_MIGRATION_TESTS')!='1',reason='Opt-in isolated PostgreSQL ordering')


def prepared(postgres,monkeypatch,entered,release,calls):
    run_id=create_run(postgres)
    with Session(postgres) as session:
        run=session.get(AssistantRun,run_id)
        conversation=session.get(AssistantConversation,run.conversation_id)
        ws,user=conversation.workspace_id,conversation.user_id
        session.add(WorkspaceMember(workspace_id=ws,user_id=user,role='editor'))
        runtime=OpenCodeRuntime(workspace_id=ws,user_id=user,container_id='a'*64,status='running')
        session.add(runtime);session.flush()
        run.opencode_session_id='provider-session'
        run.permission_response='once'
        conversation.opencode_runtime_id=runtime.id
        conversation.opencode_session_id='provider-session'
        session.commit()
        engine=module.ExpertEngine(session=session,conversation=conversation,run=run)
        connection=module.RuntimeConnection(runtime.id,'http://test-provider','a'*64)
    main_thread=threading.get_ident()
    class Provider:
        def __init__(self,*args,**kwargs): assert threading.get_ident()!=main_thread
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def respond_permission(self,*args,**kwargs):
            calls.append(args)
            entered.set()
            assert await asyncio.to_thread(release.wait,3)
            return True
    monkeypatch.setattr(module,'OpenCodeClient',Provider)
    return engine,(ws,user,connection,'provider-session','p1','once'),run_id


@pytest.mark.parametrize('change',['demote','remove'])
def test_dispatch_lock_orders_revocation_after_ack(postgres,monkeypatch,change):
    entered,release,started=threading.Event(),threading.Event(),threading.Event()
    calls=[]
    engine,args,run_id=prepared(postgres,monkeypatch,entered,release,calls)
    def revoke():
        with postgres.begin() as db:
            started.set()
            db.execute(text("UPDATE workspace_members SET role='viewer'" if change=='demote' else 'DELETE FROM workspace_members'))
    with ThreadPoolExecutor(max_workers=2) as executor:
        dispatch=executor.submit(engine._dispatch_permission,*args)
        assert entered.wait(5)
        revocation=executor.submit(revoke)
        assert started.wait(5)
        try:
            with pytest.raises(TimeoutError): revocation.result(.1)
        finally: release.set()
        dispatch.result(5);revocation.result(5)
    assert len(calls)==1
    with Session(postgres) as session:
        run=session.get(AssistantRun,run_id)
        assert run.pending_permission_id is None and run.status=='running'
        assert session.query(WorkspaceMember.role).scalar()==('viewer' if change=='demote' else None)


@pytest.mark.parametrize('change',['demote','remove','runtime','cancel','response'])
def test_invalidated_dispatch_never_calls_provider(postgres,monkeypatch,change):
    entered,release=threading.Event(),threading.Event();release.set()
    calls=[]
    engine,args,run_id=prepared(postgres,monkeypatch,entered,release,calls)
    statements={'demote':"UPDATE workspace_members SET role='viewer'",'remove':'DELETE FROM workspace_members',
        'runtime':"UPDATE opencode_runtimes SET container_id=repeat('b',64)",
        'cancel':'UPDATE assistant_runs SET cancel_requested=true',
        'response':"UPDATE assistant_runs SET permission_response='reject'"}
    with postgres.begin() as db: db.execute(text(statements[change]))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises((PermissionDeniedError,ConflictError)):
            executor.submit(engine._dispatch_permission,*args).result(5)
    assert calls==[]
    with Session(postgres) as session: assert session.get(AssistantRun,run_id).pending_permission_id=='p1'


def test_successful_dispatch_cannot_replay_the_same_sql_response(postgres,monkeypatch):
    entered,release=threading.Event(),threading.Event();release.set()
    calls=[]
    engine,args,run_id=prepared(postgres,monkeypatch,entered,release,calls)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(engine._dispatch_permission,*args).result(5)
        with pytest.raises(ConflictError): executor.submit(engine._dispatch_permission,*args).result(5)
    assert len(calls)==1
