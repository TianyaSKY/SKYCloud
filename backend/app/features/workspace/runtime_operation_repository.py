"""Caller-owned transactions for durable runtime admission and identity switch.

No Docker calls and no commits here. Admission must be committed before prepare;
candidate identity must be committed before its SQL publication is attempted.
"""

import re
import uuid
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.exceptions import ConflictError, PermissionDeniedError, ResourceNotFoundError
from app.infra.datetime_utils import utc_now
from app.infra.runtime_container_deletion import stage_container_deletion_fallback
from app.mcp.runtime_token_service import revoke_runtime_tokens
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

_ModelT = TypeVar("_ModelT")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_ACTIVE_STATES = ("preparing", "switched")


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentRuntimeParams:
    """Published runtime identity that the caller believes is current."""

    workspace_id: int
    user_id: int
    runtime_id: int
    container_id: str
    generation: str | None = None



def _locked(
    session: Session,
    model: type[_ModelT],
    *conditions: ColumnElement[bool],
) -> _ModelT | None:
    return session.scalar(
        select(model)
        .where(*conditions)
        .with_for_update()
        .execution_options(populate_existing=True),
    )


def runtime_with_write_access(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> OpenCodeRuntime | None:
    """Return the caller's runtime while holding current edit permission."""
    # Match lifecycle deletion order. Fresh reads defeat an earlier cached role.
    if _locked(session, User, User.id == user_id) is None:
        message = "Runtime owner no longer exists"
        raise ResourceNotFoundError(message)
    if _locked(session, Workspace, Workspace.id == workspace_id) is None:
        message = "Workspace no longer exists"
        raise ResourceNotFoundError(message)
    member = _locked(
        session,
        WorkspaceMember,
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user_id,
    )
    if member is None or member.role not in ("editor", "admin"):
        message = "Runtime requires current edit permission"
        raise PermissionDeniedError(message)
    return _locked(
        session,
        OpenCodeRuntime,
        OpenCodeRuntime.workspace_id == workspace_id,
        OpenCodeRuntime.user_id == user_id,
    )


def admit(session: Session, workspace_id: int, user_id: int) -> RuntimeOperation:
    """Return the active generation, admitting one when none is active."""
    runtime = runtime_with_write_access(session, workspace_id, user_id)
    if runtime is None:
        runtime = OpenCodeRuntime(
            workspace_id=workspace_id,
            user_id=user_id,
            status="stopped",
            config_version=0,
        )
        session.add(runtime)
        session.flush()
    operation = _locked(
        session,
        RuntimeOperation,
        RuntimeOperation.runtime_id == runtime.id,
        RuntimeOperation.state.in_(_ACTIVE_STATES),
    )
    if operation is None:
        operation = RuntimeOperation(
            generation=uuid.uuid4().hex,
            workspace_id=workspace_id,
            user_id=user_id,
            runtime_id=runtime.id,
            previous_container_id=runtime.container_id,
        )
        session.add(operation)
        session.flush()
    return operation


def record_candidate(
    session: Session,
    generation: str,
    container_id: object,
) -> RuntimeOperation:
    """Record the prepared container identity of one active generation."""
    if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
        message = "Candidate requires a full container identity"
        raise ConflictError(message)
    operation = _locked(
        session,
        RuntimeOperation,
        RuntimeOperation.generation == generation,
    )
    if operation is None or operation.state not in _ACTIVE_STATES:
        message = "Runtime operation is no longer active"
        raise ConflictError(message)
    if operation.candidate_container_id not in (None, container_id):
        message = "Runtime operation already has another candidate"
        raise ConflictError(message)
    operation.candidate_container_id = container_id
    session.flush()
    return operation


def switch_candidate(session: Session, generation: str) -> OpenCodeRuntime:
    """Atomically publish the recorded candidate as the runtime identity."""
    # Read IDs first, then acquire parent/runtime locks before the operation lock.
    identity = session.execute(
        select(RuntimeOperation.workspace_id, RuntimeOperation.user_id).where(
            RuntimeOperation.generation == generation,
        ),
    ).first()
    if identity is None:
        message = "Runtime operation does not exist"
        raise ResourceNotFoundError(message)
    runtime = runtime_with_write_access(session, int(identity[0]), int(identity[1]))
    operation = _locked(
        session,
        RuntimeOperation,
        RuntimeOperation.generation == generation,
    )
    if (
        runtime is None
        or operation is None
        or runtime.id != operation.runtime_id
        or operation.state not in _ACTIVE_STATES
    ):
        message = "Runtime operation is no longer active"
        raise ConflictError(message)
    candidate_id = operation.candidate_container_id
    if not candidate_id:
        message = "Runtime candidate has not been recorded"
        raise ConflictError(message)
    if operation.state == "switched":
        if runtime.container_id != candidate_id:
            message = "Runtime identity changed after switching"
            raise ConflictError(message)
        return runtime
    if runtime.container_id != operation.previous_container_id:
        message = "Runtime identity changed before switching"
        raise ConflictError(message)
    stage_container_deletion_fallback(session, runtime.container_id)
    runtime.container_id = candidate_id
    runtime.status = "provisioning"
    runtime.error_message = None
    operation.state = "switched"
    session.flush()
    return runtime


def cancel_runtime(session: Session, runtime_id: int) -> int:
    """Cancel active operations; returns how many the caller must commit durably."""
    operations = session.scalars(
        select(RuntimeOperation)
        .where(
            RuntimeOperation.runtime_id == runtime_id,
            RuntimeOperation.state.in_(_ACTIVE_STATES),
        )
        .with_for_update()
        .execution_options(populate_existing=True),
    ).all()
    for operation in operations:
        operation.state = "cancelled"
    session.flush()
    return len(operations)


def current_runtime(
    session: Session,
    params: CurrentRuntimeParams,
) -> OpenCodeRuntime:
    """Prove the runtime still carries the identity the caller prepared."""
    runtime = runtime_with_write_access(
        session,
        params.workspace_id,
        params.user_id,
    )
    if (
        runtime is None
        or runtime.id != params.runtime_id
        or runtime.container_id != params.container_id
    ):
        message = "Runtime identity is no longer current"
        raise ConflictError(message)
    cancelled = session.scalar(
        select(RuntimeOperation.generation)
        .where(
            RuntimeOperation.runtime_id == params.runtime_id,
            RuntimeOperation.candidate_container_id == params.container_id,
            RuntimeOperation.state == "cancelled",
        )
        .limit(1),
    )
    if cancelled is not None:
        message = "Runtime candidate has been cancelled"
        raise ConflictError(message)
    if params.generation is not None:
        operation = _locked(
            session,
            RuntimeOperation,
            RuntimeOperation.generation == params.generation,
        )
        if (
            operation is None
            or operation.runtime_id != params.runtime_id
            or operation.state != "switched"
            or operation.candidate_container_id != params.container_id
        ):
            message = "Runtime operation is no longer active"
            raise ConflictError(message)
    return runtime


def complete(session: Session, generation: str) -> None:
    """Mark one switched generation complete and its runtime running."""
    row = session.execute(
        select(
            RuntimeOperation.workspace_id,
            RuntimeOperation.user_id,
            RuntimeOperation.runtime_id,
            RuntimeOperation.candidate_container_id,
        ).where(RuntimeOperation.generation == generation),
    ).one()
    container_id = row[3]
    if container_id is None:
        # Same failure current_runtime reports for a missing published identity.
        message = "Runtime identity is no longer current"
        raise ConflictError(message)
    runtime = current_runtime(
        session,
        CurrentRuntimeParams(
            workspace_id=int(row[0]),
            user_id=int(row[1]),
            runtime_id=int(row[2]),
            container_id=container_id,
            generation=generation,
        ),
    )
    operation = _locked(
        session,
        RuntimeOperation,
        RuntimeOperation.generation == generation,
    )
    if operation is None:
        message = "Runtime operation is no longer active"
        raise ConflictError(message)
    runtime.status = "running"
    operation.state = "complete"
    operation.finished_at = utc_now()
    operation.last_error_type = None
    session.flush()


def replace_missing(
    session: Session,
    generation: str,
    missing_container_id: object,
) -> RuntimeOperation:
    """Drop a vanished candidate and admit the replacement generation."""
    if (
        not isinstance(missing_container_id, str)
        or not _CONTAINER_ID.fullmatch(missing_container_id)
    ):
        message = "Missing candidate requires full container identity"
        raise ConflictError(message)
    identity = session.execute(
        select(RuntimeOperation.workspace_id, RuntimeOperation.user_id).where(
            RuntimeOperation.generation == generation,
        ),
    ).one()
    workspace_id, user_id = int(identity[0]), int(identity[1])
    runtime = runtime_with_write_access(session, workspace_id, user_id)
    operation = _locked(
        session,
        RuntimeOperation,
        RuntimeOperation.generation == generation,
    )
    if (
        runtime is None
        or operation is None
        or runtime.id != operation.runtime_id
        or operation.state not in _ACTIVE_STATES
    ):
        message = "Runtime operation is no longer active"
        raise ConflictError(message)
    if operation.candidate_container_id not in (None, missing_container_id):
        message = "Missing identity does not match runtime candidate"
        raise ConflictError(message)
    if operation.state == "switched" and runtime.container_id != missing_container_id:
        message = "Runtime identity changed after switching"
        raise ConflictError(message)
    if (
        operation.state == "preparing"
        and runtime.container_id != operation.previous_container_id
    ):
        message = "Runtime identity changed before switching"
        raise ConflictError(message)
    operation.state = "cancelled"
    if runtime.container_id == missing_container_id:
        stage_container_deletion_fallback(session, missing_container_id)
        revoke_runtime_tokens(session, int(runtime.id))
        runtime.container_id = None
        runtime.status = "stopped"
    session.flush()  # Lifecycle triggers cannot cancel the new admission.
    return admit(session, workspace_id, user_id)
