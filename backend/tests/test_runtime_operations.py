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
