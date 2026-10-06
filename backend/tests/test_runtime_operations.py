"""Actual SQL admission/switch/rollback and durable retirement contracts."""
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.exceptions import ConflictError, PermissionDeniedError
from app.features.workspace import runtime_operation_repository as repository
from app.infra.datetime_utils import utc_now
from app.infra.runtime_operation_cleanup import retire_cancelled_operations
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation
from app.models.runtime_container_deletion import RuntimeContainerDeletion
from app.models.workspace import WorkspaceMember


def test_candidate_switch_rollback_keeps_prior_identity_and_candidate_intent(session,test_workspace,test_user):
    runtime = OpenCodeRuntime(workspace_id=test_workspace.id,user_id=test_user.id,container_id='a'*64,status='running')
    session.add(runtime)
    session.commit()
    operation = repository.admit(session,test_workspace.id,test_user.id)
    generation = operation.generation
    session.commit()
    repository.record_candidate(session,generation,'b'*64)
    session.commit()
    repository.switch_candidate(session,generation)
    assert runtime.container_id == 'b'*64
    session.rollback()
    assert runtime.container_id == 'a'*64
    assert operation.state == 'preparing' and operation.candidate_container_id == 'b'*64
    assert session.query(RuntimeContainerDeletion).count() == 0
    repository.switch_candidate(session,generation)
    session.commit()
    assert runtime.container_id == 'b'*64 and operation.state == 'switched'
    assert session.query(RuntimeContainerDeletion).one().container_id == 'a'*64
    assert repository.switch_candidate(session,generation).id == runtime.id


def test_admission_replays_one_operation_and_fresh_role_denies_switch(session,test_workspace,test_user):
    operation = repository.admit(session,test_workspace.id,test_user.id)
    session.commit()
    assert repository.admit(session,test_workspace.id,test_user.id).generation == operation.generation
    repository.record_candidate(session,operation.generation,'b'*64)
    session.commit()
    member = session.query(WorkspaceMember).one()
    session.execute(update(WorkspaceMember).values(role='viewer'),execution_options={'synchronize_session':False})
    session.commit()
    with pytest.raises(PermissionDeniedError): repository.switch_candidate(session,operation.generation)
    assert member.role == 'viewer'


def test_cancelled_operation_cannot_publish_or_accept_late_candidate(session,test_workspace,test_user):
    operation = repository.admit(session,test_workspace.id,test_user.id)
    session.commit()
    repository.cancel_runtime(session,operation.runtime_id)
    session.commit()
    with pytest.raises(ConflictError): repository.record_candidate(session,operation.generation,'b'*64)
    with pytest.raises(ConflictError): repository.switch_candidate(session,operation.generation)
    assert session.query(OpenCodeRuntime).one().container_id is None


def test_retirement_failure_keeps_identity_and_replays_after_owner_deleted(session,test_workspace,test_user):
    operation = repository.admit(session,test_workspace.id,test_user.id)
    session.commit()
    repository.cancel_runtime(session,operation.runtime_id)
    session.query(OpenCodeRuntime).delete()
    session.commit()
    factory=lambda:Session(bind=session.get_bind(),join_transaction_mode='create_savepoint')
    def reject(*args): raise RuntimeError('private detail')
    assert retire_cancelled_operations(session_factory=factory,controller_factory=lambda:SimpleNamespace(retire_generation=reject)) == 0
    session.expire_all()
    assert operation.finished_at is None and operation.last_error_type == 'RuntimeError' and operation.attempts == 1
    operation.next_attempt_at = utc_now()
    session.commit()
    calls=[]
    assert retire_cancelled_operations(session_factory=factory,controller_factory=lambda:SimpleNamespace(retire_generation=lambda *args:calls.append(args))) == 1
    session.expire_all()
    assert calls == [(test_workspace.id,test_user.id,operation.runtime_id,operation.generation)]
    assert operation.finished_at is not None and operation.last_error_type is None


@pytest.fixture
def provisioner(session,test_workspace,test_user,monkeypatch):
    from app.features.workspace import docker_service
    from runtime_controller.policy import POLICY_VERSION
    containers = {}
    calls = []
    class Container:
        def __init__(self,identity):
            self.id = identity
            self.status = 'created'
            self.attrs = {'Config':{'Image':docker_service.OPENCODE_IMAGE,'Labels':{'skycloud.runtime_policy':POLICY_VERSION}}}
            self.config = {}
        def start(self): self.status = 'running'
        def read_config(self): return self.config
        def write_config(self,config): self.config = config
    class Controller:
        containers = SimpleNamespace(get=lambda identity: containers[identity])
        def prepare(self,workspace,user,runtime,generation):
            calls.append(generation)
            containers.setdefault('b'*64,Container('b'*64))
            return containers['b'*64]
    monkeypatch.setattr(docker_service,'_client',Controller)
    monkeypatch.setattr(docker_service,'_sync_mcp_via_runtime_api',lambda *args,**kwargs:None)
    return containers,calls


def test_production_start_prepares_publishes_configures_and_completes(session,test_workspace,test_user,provisioner):
    from app.features.workspace import docker_service
    from app.models.mcp_runtime_token import McpRuntimeToken
    containers,calls = provisioner
    docker_service.start(session,test_workspace,test_user.id)
    operation = session.query(RuntimeOperation).one()
    runtime = session.query(OpenCodeRuntime).one()
    assert calls == [operation.generation]
    assert operation.state == 'complete' and operation.finished_at is not None
    assert runtime.container_id == 'b'*64 and runtime.status == 'running' and runtime.config_version == 1
    assert len(session.query(McpRuntimeToken).filter_by(revoked_at=None).all()) == 2
    assert 'SKYCLOUD' in containers['b'*64].config['mcp']


def test_worker_recovers_committed_admission_with_same_generation(session,test_workspace,test_user,provisioner):
    from app.infra.runtime_operations import recover_runtime_operations
    operation = repository.admit(session,test_workspace.id,test_user.id)
    generation = operation.generation
    session.commit()  # Simulate death before prepare.
    factory=lambda:Session(bind=session.get_bind(),join_transaction_mode='create_savepoint')
    assert recover_runtime_operations(session_factory=factory) == 1
    session.expire_all()
    assert operation.state == 'complete' and provisioner[1] == [generation]


def test_configuration_rechecks_cancel_after_token_commit(session,test_workspace,test_user,provisioner,monkeypatch):
    from app.features.workspace import docker_service
    from app.features.runtime_llm import tokens
    issue = tokens.issue_token
    def cancel_after_issue(db,runtime):
        token = issue(db,runtime)
        repository.cancel_runtime(db,runtime.id)
        db.commit()
        return token
    monkeypatch.setattr(tokens,'issue_token',cancel_after_issue)
    with pytest.raises(ConflictError):
        docker_service.start(session,test_workspace,test_user.id)
    assert session.query(RuntimeOperation).one().state == 'cancelled'
    assert provisioner[0]['b'*64].config == {}


def test_stale_worker_generation_cannot_start_completed_or_new_operation(session,test_workspace,test_user,provisioner):
    from app.features.workspace import docker_service
    docker_service.start(session,test_workspace,test_user.id,expected_generation='f'*32)
    assert provisioner[1] == [] and session.query(RuntimeOperation).count() == 0


def test_cancelled_candidate_cannot_receive_configuration_as_current_runtime(session,test_workspace,test_user,provisioner):
    from app.features.workspace import docker_service
    docker_service.start(session,test_workspace,test_user.id)
    operation = session.query(RuntimeOperation).one()
    runtime = session.query(OpenCodeRuntime).one()
    operation.state = 'cancelled'
    session.commit()
    before = dict(provisioner[0]['b'*64].config)
    with pytest.raises(ConflictError):
        docker_service.setup_mcp(session,test_workspace,test_user.id,runtime=runtime)
    assert provisioner[0]['b'*64].config == before


def test_summary_observation_cannot_cancel_pending_start(session,test_workspace,test_user,provisioner):
    from app.features.workspace import docker_service
    operation = repository.admit(session,test_workspace.id,test_user.id)
    session.commit()
    repository.record_candidate(session,operation.generation,'b'*64)
    session.commit()
    runtime = repository.switch_candidate(session,operation.generation)
    session.commit()
    docker_service._sync_runtime_status(session,runtime)
    assert runtime.status == 'provisioning'
    # Batch snapshot says candidate is stopped. The conditional SQL update
    # must leave an admitted operation's identity/status untouched.
    class Client:
        def user_snapshot(self,user): return []
    from unittest.mock import patch
    with patch.object(docker_service,'_client',Client):
        docker_service.list_summaries(session,[({'id':test_workspace.id},runtime)],test_user.id)
    session.expire_all()
    assert runtime.status == 'provisioning' and runtime.container_id == 'b'*64
    assert operation.state == 'switched'


def test_missing_candidate_replacement_cancels_old_and_preserves_previous_container(session,test_workspace,test_user):
    runtime=OpenCodeRuntime(workspace_id=test_workspace.id,user_id=test_user.id,container_id='a'*64,status='running')
    session.add(runtime); session.commit()
    old=repository.admit(session,test_workspace.id,test_user.id); session.commit()
    repository.record_candidate(session,old.generation,'b'*64); session.commit()
    replacement=repository.replace_missing(session,old.generation,'b'*64)
    session.commit()
    assert old.state=='cancelled' and replacement.generation!=old.generation
    assert replacement.previous_container_id=='a'*64 and runtime.container_id=='a'*64
    assert session.query(RuntimeContainerDeletion).count()==0


def test_missing_switched_candidate_replacement_rollback_is_atomic(session,test_workspace,test_user):
    old=repository.admit(session,test_workspace.id,test_user.id); session.commit()
    repository.record_candidate(session,old.generation,'b'*64); session.commit()
    runtime=repository.switch_candidate(session,old.generation); session.commit()
    replacement=repository.replace_missing(session,old.generation,'b'*64)
    generation=replacement.generation
    assert runtime.container_id is None and old.state=='cancelled'
    session.rollback()
    assert runtime.container_id=='b'*64 and old.state=='switched'
    assert session.get(RuntimeOperation,generation) is None
    assert session.query(RuntimeContainerDeletion).count()==0


def test_missing_candidate_cannot_replace_another_identity(session,test_workspace,test_user):
    old=repository.admit(session,test_workspace.id,test_user.id); session.commit()
    repository.record_candidate(session,old.generation,'b'*64); session.commit()
    with pytest.raises(ConflictError): repository.replace_missing(session,old.generation,'c'*64)
    assert old.state=='preparing' and session.query(RuntimeOperation).count()==1
