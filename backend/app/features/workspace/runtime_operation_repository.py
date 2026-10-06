"""Caller-owned transactions for durable runtime admission and identity switch.

No Docker calls and no commits here. Admission must be committed before prepare;
candidate identity must be committed before its SQL publication is attempted.
"""
import re
import uuid

from sqlalchemy import select

from app.exceptions import ConflictError, PermissionDeniedError, ResourceNotFoundError
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation
from app.infra.runtime_container_deletion import stage_container_deletion_fallback
from app.infra.datetime_utils import utc_now


def _locked(session, model, *conditions):
    return session.scalar(select(model).where(*conditions).with_for_update().execution_options(populate_existing=True))


def _runtime_with_write_access(session, workspace_id, user_id):
    # Match lifecycle deletion order. Fresh reads defeat an earlier cached role.
    if _locked(session, User, User.id == user_id) is None:
        raise ResourceNotFoundError('Runtime owner no longer exists')
    if _locked(session, Workspace, Workspace.id == workspace_id) is None:
        raise ResourceNotFoundError('Workspace no longer exists')
    member = _locked(session, WorkspaceMember, WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
    if member is None or member.role not in ('editor', 'admin'):
        raise PermissionDeniedError('Runtime requires current edit permission')
    return _locked(session, OpenCodeRuntime, OpenCodeRuntime.workspace_id == workspace_id, OpenCodeRuntime.user_id == user_id)


def admit(session, workspace_id, user_id):
    runtime = _runtime_with_write_access(session, workspace_id, user_id)
    if runtime is None:
        runtime = OpenCodeRuntime(workspace_id=workspace_id, user_id=user_id, status='stopped', config_version=0)
        session.add(runtime)
        session.flush()
    operation = _locked(session, RuntimeOperation, RuntimeOperation.runtime_id == runtime.id, RuntimeOperation.state.in_(['preparing','switched']))
    if operation is None:
        operation = RuntimeOperation(generation=uuid.uuid4().hex, workspace_id=workspace_id, user_id=user_id,
                                     runtime_id=runtime.id, previous_container_id=runtime.container_id)
        session.add(operation)
        session.flush()
    return operation


def record_candidate(session, generation, container_id):
    if not isinstance(container_id, str) or not re.fullmatch(r'[0-9a-f]{64}', container_id):
        raise ConflictError('Candidate requires a full container identity')
    operation = _locked(session, RuntimeOperation, RuntimeOperation.generation == generation)
    if operation is None or operation.state not in ('preparing','switched'):
        raise ConflictError('Runtime operation is no longer active')
    if operation.candidate_container_id not in (None, container_id):
        raise ConflictError('Runtime operation already has another candidate')
    operation.candidate_container_id = container_id
    session.flush()
    return operation


def switch_candidate(session, generation):
    # Read IDs first, then acquire parent/runtime locks before the operation lock.
    identity = session.execute(select(RuntimeOperation.workspace_id, RuntimeOperation.user_id).where(RuntimeOperation.generation == generation)).first()
    if identity is None:
        raise ResourceNotFoundError('Runtime operation does not exist')
    runtime = _runtime_with_write_access(session, *identity)
    operation = _locked(session, RuntimeOperation, RuntimeOperation.generation == generation)
    if runtime is None or runtime.id != operation.runtime_id or operation.state not in ('preparing','switched'):
        raise ConflictError('Runtime operation is no longer active')
    if not operation.candidate_container_id:
        raise ConflictError('Runtime candidate has not been recorded')
    if operation.state == 'switched':
        if runtime.container_id != operation.candidate_container_id:
            raise ConflictError('Runtime identity changed after switching')
        return runtime
    if runtime.container_id != operation.previous_container_id:
        raise ConflictError('Runtime identity changed before switching')
    stage_container_deletion_fallback(session, runtime.container_id)
    runtime.container_id = operation.candidate_container_id
    runtime.status = 'provisioning'
    runtime.error_message = None
    operation.state = 'switched'
    session.flush()
    return runtime


def cancel_runtime(session, runtime_id):
    operations = session.scalars(select(RuntimeOperation).where(RuntimeOperation.runtime_id == runtime_id,
        RuntimeOperation.state.in_(['preparing','switched'])).with_for_update().execution_options(populate_existing=True)).all()
    for operation in operations:
        operation.state = 'cancelled'
    session.flush()


def current_runtime(session, workspace_id, user_id, runtime_id, container_id, generation=None):
    runtime = _runtime_with_write_access(session, workspace_id, user_id)
    if runtime is None or runtime.id != runtime_id or runtime.container_id != container_id:
        raise ConflictError('Runtime identity is no longer current')
    if session.scalar(select(RuntimeOperation.generation).where(RuntimeOperation.runtime_id == runtime_id,
            RuntimeOperation.candidate_container_id == container_id,RuntimeOperation.state == 'cancelled').limit(1)) is not None:
        raise ConflictError('Runtime candidate has been cancelled')
    if generation is not None:
        operation = _locked(session, RuntimeOperation, RuntimeOperation.generation == generation)
        if operation is None or operation.runtime_id != runtime_id or operation.state != 'switched' or operation.candidate_container_id != container_id:
            raise ConflictError('Runtime operation is no longer active')
    return runtime


def complete(session, generation):
    identity = session.execute(select(RuntimeOperation.workspace_id, RuntimeOperation.user_id,
        RuntimeOperation.runtime_id, RuntimeOperation.candidate_container_id).where(RuntimeOperation.generation == generation)).one()
    runtime = current_runtime(session, *identity, generation=generation)
    operation = _locked(session, RuntimeOperation, RuntimeOperation.generation == generation)
    runtime.status = 'running'
    operation.state = 'complete'
    operation.finished_at = utc_now()
    operation.last_error_type = None
    session.flush()


def replace_missing(session, generation, missing_container_id):
    if not isinstance(missing_container_id,str) or not re.fullmatch(r'[0-9a-f]{64}',missing_container_id):
        raise ConflictError('Missing candidate requires full container identity')
    identity = session.execute(select(RuntimeOperation.workspace_id,RuntimeOperation.user_id)
        .where(RuntimeOperation.generation == generation)).one()
    runtime = _runtime_with_write_access(session,*identity)
    operation = _locked(session,RuntimeOperation,RuntimeOperation.generation == generation)
    if runtime is None or runtime.id != operation.runtime_id or operation.state not in ('preparing','switched'):
        raise ConflictError('Runtime operation is no longer active')
    if operation.candidate_container_id not in (None,missing_container_id):
        raise ConflictError('Missing identity does not match runtime candidate')
    if operation.state == 'switched' and runtime.container_id != missing_container_id:
        raise ConflictError('Runtime identity changed after switching')
    if operation.state == 'preparing' and runtime.container_id != operation.previous_container_id:
        raise ConflictError('Runtime identity changed before switching')
    operation.state = 'cancelled'
    if runtime.container_id == missing_container_id:
        from app.mcp.runtime_token_service import revoke_runtime_tokens
        stage_container_deletion_fallback(session,missing_container_id)
        revoke_runtime_tokens(session,int(runtime.id))
        runtime.container_id = None
        runtime.status = 'stopped'
    session.flush()  # Lifecycle triggers cannot cancel the new admission.
    return admit(session,*identity)
