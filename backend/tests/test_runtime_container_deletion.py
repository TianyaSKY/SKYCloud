"""Transactional fallback and cleanup identity contracts using actual SQLite."""
from types import SimpleNamespace
from sqlalchemy.orm import Session
from app.features.workspace.docker_service import remove_runtime
from app.infra.datetime_utils import utc_now
from app.infra.runtime_container_deletion import delete_pending_containers
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_container_deletion import RuntimeContainerDeletion
from app.models.mcp_runtime_token import McpRuntimeToken
from runtime_controller.client import ControllerNotFound


def reaper(session, factory):
    return delete_pending_containers(session_factory=lambda:Session(bind=session.get_bind(),join_transaction_mode='create_savepoint'),controller_factory=factory)


def seed_runtime(session,workspace,user):
    runtime = OpenCodeRuntime(workspace_id=workspace.id,user_id=user.id,container_id='a'*64,status='running')
    session.add(runtime)
    session.flush()
    token = McpRuntimeToken(runtime_id=runtime.id,workspace_id=workspace.id,user_id=user.id,jti='cleanup-jti',token_hash='cleanup-hash',token_preview='p',expires_at=utc_now())
    session.add(token)
    session.commit()
    return runtime,token


def test_detach_and_credential_revocation_rollback_together(session,test_workspace,test_user,monkeypatch):
    from app.features.workspace import docker_service
    monkeypatch.setattr(docker_service,'_client',lambda:(_ for _ in ()).throw(AssertionError('RPC before commit')))
    runtime,token = seed_runtime(session,test_workspace,test_user)
    remove_runtime(runtime,session)
    assert runtime.container_id is None and token.revoked_at is not None
    assert session.query(RuntimeContainerDeletion).count() == 1
    session.rollback()
    assert runtime.container_id == 'a'*64 and token.revoked_at is None
    assert session.query(RuntimeContainerDeletion).count() == 0


def test_controller_failure_persists_type_and_missing_replay_completes(session):
    session.add(RuntimeContainerDeletion(container_id='a'*64))
    session.commit()
    def reject(ref): raise RuntimeError('sensitive details must not be retained')
    assert reaper(session,lambda:SimpleNamespace(containers=SimpleNamespace(get=reject))) == 0
    session.expire_all()
    row = session.query(RuntimeContainerDeletion).one()
    assert row.attempts == 1 and row.last_error_type == 'RuntimeError' and row.completed_at is None
    row.next_attempt_at = utc_now()
    session.commit()
    def missing(ref): raise ControllerNotFound('already gone')
    assert reaper(session,lambda:SimpleNamespace(containers=SimpleNamespace(get=missing))) == 1
    session.expire_all()
    assert row.completed_at is not None and row.last_error_type is None and row.attempts == 2


def test_alias_reference_cannot_remove_a_different_full_identity(session):
    removed = []
    session.add(RuntimeContainerDeletion(container_id='a'*12))
    session.commit()
    container = SimpleNamespace(id='a'*64,remove=lambda **kwargs:removed.append(True))
    assert reaper(session,lambda:SimpleNamespace(containers=SimpleNamespace(get=lambda ref:container))) == 0
    assert removed == []
    session.expire_all()
    row = session.query(RuntimeContainerDeletion).one()
    assert row.completed_at is None and row.last_error_type == 'ControllerError'


def test_live_reference_is_retained_then_detach_stages_new_cleanup(session,test_workspace,test_user):
    runtime,_ = seed_runtime(session,test_workspace,test_user)
    session.add(RuntimeContainerDeletion(container_id=runtime.container_id))
    session.commit()
    removed = []
    container = SimpleNamespace(id='a'*64,remove=lambda **kwargs:removed.append(True))
    factory = lambda:SimpleNamespace(containers=SimpleNamespace(get=lambda ref:container))
    assert reaper(session,factory) == 1 and removed == []
    remove_runtime(runtime,session)
    session.commit()
    assert reaper(session,factory) == 1 and removed == [True]


def test_old_controller_missing_legacy_route_keeps_cleanup_pending(session,monkeypatch):
    import httpx
    from runtime_controller.client import ControllerClient
    monkeypatch.setenv('RUNTIME_CONTROLLER_TOKEN','T'*48)
    def response(method,url,**kwargs):
        if '/retired-containers/' in url:
            return httpx.Response(404,json={'detail':'Not Found'})
        return httpx.Response(403,json={'detail':'Historical identity rejected'})
    monkeypatch.setattr(httpx,'request',response)
    session.add(RuntimeContainerDeletion(container_id='a'*64))
    session.commit()
    assert reaper(session,ControllerClient) == 0
    session.expire_all()
    row = session.query(RuntimeContainerDeletion).one()
    assert row.completed_at is None and row.attempts == 1 and row.last_error_type == 'ControllerError'
