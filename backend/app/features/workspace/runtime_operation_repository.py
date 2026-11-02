"""调用方持有事务的持久化 Runtime 准入与代际身份切换仓库.

本层不发起 Docker 调用且不执行 commit。准入必须在 prepare 前提交;
候选代际身份必须在尝试 SQL 发布之前完成提交。
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
    """调用方认为处于当前有效状态的已发布运行时身份."""

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
    """在持有当前编辑权限的同时返回调用方的运行时记录."""
    # 遵循生命周期删除顺序。强制最新读取以绕过历史缓存的角色。
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
    """返回活跃代际标识, 当不存在活跃代际时准入新代际."""
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
    """记录单个活跃代际已准备好的容器身份信息."""
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
    """原子性地将记录在案的候选容器发布为正式运行时身份."""
    # 先读取相关 ID, 随后在获取操作锁前先获取父级/运行时锁。
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
    """取消活跃操作; 返回调用方必须持久化提交的已取消数量."""
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
    """确认运行时依然承载着调用方所准备的代际身份."""
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
    """将已切换的代际标记为已完成并将运行时置为 running 状态."""
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
        # 与 current_runtime 在缺少已发布代际身份时返回的失败保持一致。
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
    """丢弃已消失的候选代际记录并准入替代的全新代际."""
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
    session.flush()  # 生命周期触发器不能取消新准入的操作。
    return admit(session, workspace_id, user_id)
