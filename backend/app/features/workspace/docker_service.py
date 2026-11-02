"""OpenCode Docker 容器生命周期与各运行时 MCP 配置管理."""

import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TypedDict

import httpx
from loguru import logger
from runtime_common.contracts import ContainerPayload
from runtime_common.json_types import JsonObject
from runtime_controller.client import (
    ControllerClient,
    ControllerError,
    ControllerGenerationMissing,
    ControllerNotFound,
    runtime_url,
)
from runtime_controller.client import (
    auth_headers as controller_auth_headers,
)
from runtime_controller.policy import POLICY_VERSION
from sqlalchemy import case, exists, select, update
from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError, ConflictError
from app.features.assistant.clients.opencode_auth import (
    server_password,
    server_username,
)
from app.features.runtime_llm import tokens as runtime_llm_tokens
from app.features.workspace import (
    runtime_config,
    runtime_config_client,
    runtime_service,
)
from app.features.workspace import runtime_operation_repository as operations
from app.features.workspace.permissions import get_member_role
from app.features.workspace.runtime_execution import serialized
from app.features.workspace.runtime_port import RuntimeController
from app.features.workspace.schemas import (
    RuntimeSummary,
    WorkspaceListEntry,
    WorkspaceListItemData,
)
from app.infra import runtime_operations
from app.infra.datetime_utils import utc_now
from app.infra.llm.config import get_chat_model_config
from app.infra.runtime_container_deletion import stage_container_deletion_fallback
from app.mcp import runtime_token_service
from app.models.mcp_runtime_token import McpRuntimeToken
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation
from app.models.workspace import Workspace

# glibc 运行时构建器会将不可变本地 ID 写入私有 .env 文件中。
OPENCODE_IMAGE = os.getenv("OPENCODE_IMAGE", "")
MCP_PORT = int(os.getenv("MCP_PORT", "5001"))
OPENCODE_EXPERT_AGENT = os.getenv("OPENCODE_EXPERT_AGENT", "skycloud-expert")
OPENCODE_CHAT_PROVIDER_ID = os.getenv("OPENCODE_CHAT_PROVIDER_ID", "skycloud-chat")
DEFAULT_MCP_URL = "http://skycloud-backend-mcp:{port}/mcp"
DEFAULT_LLM_GATEWAY_URL = "http://skycloud-llm-gateway:5002/v1"
DEFAULT_STARTUP_TIMEOUT = 30.0
RUNTIME_POLICY_LABEL = "skycloud.runtime_policy"

NOT_RUNNING = "SKYcode 工作区未运行"
RUNTIME_NOT_CREATED = "Runtime has not been created"
MEMBER_REQUIRED = "用户不是该工作空间成员"


class ModelConfigPolicy(TypedDict):
    """Runtime Chat 模型供应商的 Token 上限与供应商标识策略."""

    provider_id: str
    output_limit: int
    context_limit: int


@dataclass(frozen=True, slots=True)
class StartAttempt:
    """单次启动尝试所请求的代际边界上下文."""

    expected_generation: str | None = None
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class CandidateReplacement:
    """在发布前已消失的已准备候选代际身份."""

    generation: str
    container_id: str
    attempt: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeConfigSync:
    """向运行中的 Runtime 推送 MCP/供应商配置的同步选项."""

    reload_config: bool = False
    runtime_config: "JsonObject | None" = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeProvisioning:
    """单次 Runtime 配置写入的配置编排上下文."""

    runtime: OpenCodeRuntime | None = None
    reload_config: bool = False
    generation: str | None = None


@dataclass(slots=True)
class StartState:
    """单次启动尝试的可变进度跟踪状态."""

    workspace_id: int
    user_id: int
    runtime: OpenCodeRuntime | None
    generation: str | None = None


def _client() -> RuntimeController:
    return ControllerClient()




def _container_uses_configured_image(container: ContainerPayload) -> bool:
    """现有 Runtime 是否由当前镜像引用创建而成."""
    if container["labels"].get(RUNTIME_POLICY_LABEL) != POLICY_VERSION:
        return False
    image_ref = container["image"]
    return not image_ref or image_ref == OPENCODE_IMAGE

def runtime_uses_configured_image(runtime: OpenCodeRuntime) -> bool:
    """检查已持久化的 Runtime 是否已在使用 ``OPENCODE_IMAGE``.

    在镜像配置变更前创建的 Runtime 应在下次使用时重新构建,
    而不是继续运行陈旧的项目历史镜像。
    """
    container_id = runtime.container_id
    if not container_id:
        return False
    try:
        container = _client().inspect(container_id)
    except ControllerNotFound:
        return False
    except ControllerError as exc:
        logger.warning(
            "无法检查 OpenCode Runtime 镜像: runtime_id={}, error_type={}",
            runtime.id,
            type(exc).__name__,
        )
        # 绝不可仅因 Docker 暂时不可用就销毁健康的 Runtime;
        # 调用方会正常向上抛出对应的常规错误。
        return True
    return _container_uses_configured_image(container)


def get_runtime_base_url(
    workspace: Workspace,
    user_id: int,
    runtime: OpenCodeRuntime | None = None,
) -> str:
    """返回适用于 Docker 容器与混合本地部署的 Runtime URL.

    容器化及本地 API 进程均通过经鉴权的 Controller 中继进行中转,
    绝不需要直接暴露 Runtime 网络接口或主机端口。
    """
    configured = os.getenv("OPENCODE_RUNTIME_BASE_URL") or os.getenv(
        "OPENCODE_SERVER_URL",
    )
    if configured:
        try:
            return configured.format(
                workspace_id=int(workspace.id),
                user_id=int(user_id),
                runtime_id=int(runtime.id) if runtime and runtime.id else "",
            ).rstrip("/")
        except (KeyError, ValueError):
            return configured.rstrip("/")

    if runtime is not None and runtime.container_id:
        return runtime_url(runtime.container_id)
    raise BusinessRuleError(RUNTIME_NOT_CREATED)


def _mcp_endpoint() -> str:
    configured = os.getenv("OPENCODE_MCP_URL")
    if configured:
        return configured.rstrip("/")
    return DEFAULT_MCP_URL.format(port=MCP_PORT)


def _startup_timeout_seconds() -> float:
    try:
        return max(float(os.getenv("OPENCODE_STARTUP_TIMEOUT", "30")), 1.0)
    except ValueError:
        return DEFAULT_STARTUP_TIMEOUT


def _sync_mcp_via_runtime_api(
    workspace: Workspace,
    user_id: int,
    runtime: OpenCodeRuntime,
    config: JsonObject,
    sync: RuntimeConfigSync,
) -> None:
    """无需重启容器, 直接向运行中的 Runtime 应用最新的 MCP 凭据."""
    base_url = get_runtime_base_url(workspace, user_id, runtime)
    auth = httpx.BasicAuth(
        server_username(),
        server_password(int(runtime.id), int(user_id), int(workspace.id)),
    )
    runtime_config_client.sync_mcp(
        base_url,
        auth,
        controller_auth_headers(base_url),
        config,
        runtime_config_client.ConfigSyncOptions(
            startup_timeout=_startup_timeout_seconds(),
            reload_config=sync.reload_config,
            runtime_config=sync.runtime_config,
        ),
    )



def _sync_status(session: Session, workspace: Workspace) -> None:
    """针对旧版工作空间容器字段的尽力而为同步."""
    container_id = workspace.container_id
    if not container_id:
        return
    try:
        container = _client().inspect(container_id)
        status = "running" if container["status"] == "running" else "stopped"
        if workspace.status != status:
            workspace.status = status
            session.commit()
    except ControllerNotFound:
        workspace.container_id = None
        workspace.status = "stopped"
        session.commit()
    except ControllerError as exc:
        logger.warning(
            "同步 OpenCode 容器状态失败: workspace_id={}, error_type={}",
            workspace.id,
            type(exc).__name__,
        )


def _sync_runtime_status(session: Session, runtime: OpenCodeRuntime) -> None:
    container_id = runtime.container_id
    if not container_id:
        return
    active = (
        session.query(RuntimeOperation.generation)
        .filter(
            RuntimeOperation.runtime_id == runtime.id,
            RuntimeOperation.state.in_(["preparing", "switched"]),
        )
        .first()
    )
    if active is not None:
        return  # 观测到已停止的候选容器不代表是用户主动发起的停止请求。
    try:
        container = _client().inspect(container_id)
        status = "running" if container["status"] == "running" else "stopped"
        if runtime.status != status:
            runtime.status = status
            runtime.last_used_at = utc_now()
            session.commit()
    except ControllerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
        runtime.last_used_at = utc_now()
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
    except ControllerError as exc:
        logger.warning(
            "同步 OpenCode Runtime 状态失败: runtime_id={}, error_type={}",
            runtime.id,
            type(exc).__name__,
        )


def _runtime_summary(
    session: Session,
    runtime: OpenCodeRuntime,
) -> RuntimeSummary:
    _sync_runtime_status(session, runtime)
    container_id = runtime.container_id
    return RuntimeSummary(
        runtime_id=int(runtime.id),
        user_id=int(runtime.user_id),
        container_id=container_id[:12] if container_id else None,
        status=runtime.status,
        error_message=runtime.error_message,
        config_version=runtime.config_version,
        access_url=get_access_url(runtime) if runtime.status == "running" else None,
    )


def _runtime_error_message(exc: Exception) -> str:
    message = str(exc)
    if "No such image" in message or "pull access denied" in message:
        return (
            f"无法获取 OpenCode 官方镜像({OPENCODE_IMAGE}). "
            "请检查 Docker Desktop 是否能访问 ghcr.io, "
            f"或手动执行: docker pull {OPENCODE_IMAGE}"
        )
    return "Runtime 操作失败, 请检查服务器日志"


def summary(
    session: Session,
    workspace: Workspace,
    user_id: int | None = None,
) -> RuntimeSummary:
    """返回当前成员的 Runtime 状态概要.

    工作空间层级的旧字段保留为只读兼容性回退,
    供在引入 ``opencode_runtimes`` 表之前部署的历史环境使用。
    """
    if user_id is not None:
        runtime = runtime_service.get_runtime(session, int(workspace.id), int(user_id))
        if runtime:
            return _runtime_summary(session, runtime)

    _sync_status(session, workspace)
    container_id = workspace.container_id
    return RuntimeSummary(
        runtime_id=None,
        user_id=user_id,
        container_id=container_id[:12] if container_id else None,
        status=workspace.status,
        error_message=workspace.error_message,
        config_version=None,
        access_url=get_access_url(workspace) if workspace.status == "running" else None,
    )


def _status_snapshot(
    user_id: int,
) -> dict[tuple[int, int, str], str] | None:
    try:
        return {
            (int(row["runtime_id"]), int(row["workspace_id"]), row["id"]): row[
                "status"
            ]
            for row in _client().user_snapshot(user_id)
            if int(row["user_id"]) == user_id
        }
    except ControllerError as exc:
        logger.warning(
            "批量同步 OpenCode 状态失败: user_id={}, error_type={}",
            user_id,
            type(exc).__name__,
        )
        return None


@dataclass(slots=True)
class _ListChanges:
    """等待批量 SQL 同步写入的已观测运行时状态变更."""

    status: dict[int, str]
    container: dict[int, str | None]
    missing: list[int]


def _list_item(
    workspace: WorkspaceListEntry,
    runtime: OpenCodeRuntime | None,
    user_id: int,
    snapshot: dict[tuple[int, int, str], str] | None,
    changes: _ListChanges,
) -> WorkspaceListItemData:
    if runtime is None:
        return WorkspaceListItemData(
            **workspace,
            runtime_id=None,
            user_id=user_id,
            config_version=None,
            access_url=None,
        )
    status, container_id = runtime.status, runtime.container_id
    if snapshot is not None and container_id:
        key = (int(runtime.id), int(runtime.workspace_id), container_id)
        status = snapshot.get(key, "stopped")
        if key not in snapshot:
            container_id = None
            changes.missing.append(int(runtime.id))
        if status != runtime.status or container_id != runtime.container_id:
            changes.status[runtime.id] = status
            changes.container[runtime.id] = container_id
    item = WorkspaceListItemData(
        **workspace,
        runtime_id=int(runtime.id),
        user_id=user_id,
        config_version=runtime.config_version,
        access_url=None,
    )
    item["container_id"] = container_id[:12] if container_id else None
    item["status"] = status
    item["error_message"] = runtime.error_message
    return item


def _apply_status_changes(
    session: Session,
    runtimes: list[OpenCodeRuntime],
    changes: _ListChanges,
) -> None:
    if not changes.status:
        return
    now = utc_now()
    # 亦需比对观测到的容器, 并发的重新创建绝不能被旧快照
    # 覆盖, 其新凭据亦绝不可被陈旧快照吊销。
    observed = [runtime for runtime in runtimes if runtime.id in changes.status]
    updated = (
        session.execute(
            update(OpenCodeRuntime)
            .where(
                OpenCodeRuntime.id.in_([runtime.id for runtime in observed]),
                ~exists(
                    select(RuntimeOperation.generation).where(
                        RuntimeOperation.runtime_id == OpenCodeRuntime.id,
                        RuntimeOperation.state.in_(["preparing", "switched"]),
                    ),
                ),
                OpenCodeRuntime.container_id == case(
                    {
                        int(runtime.id): runtime.container_id
                        for runtime in observed
                    },
                    value=OpenCodeRuntime.id,
                ),
                OpenCodeRuntime.status == case(
                    {int(runtime.id): runtime.status for runtime in observed},
                    value=OpenCodeRuntime.id,
                ),
                OpenCodeRuntime.updated_at == case(
                    {int(runtime.id): runtime.updated_at for runtime in observed},
                    value=OpenCodeRuntime.id,
                ),
            )
            .values(
                status=case(changes.status, value=OpenCodeRuntime.id),
                container_id=case(changes.container, value=OpenCodeRuntime.id),
                last_used_at=now,
            )
            .returning(OpenCodeRuntime.id),
            execution_options={"synchronize_session": False},
        )
        .scalars()
        .all()
    )
    removed = set(updated).intersection(changes.missing)
    if removed:
        session.execute(
            update(McpRuntimeToken)
            .where(
                McpRuntimeToken.runtime_id.in_(removed),
                McpRuntimeToken.revoked_at.is_(None),
            )
            .values(revoked_at=now),
            execution_options={"synchronize_session": False},
        )
    session.commit()


def list_summaries(
    session: Session,
    rows: list[tuple[WorkspaceListEntry, OpenCodeRuntime | None]],
    user_id: int,
) -> list[WorkspaceListItemData]:
    """以常量级数据库与 Controller 请求刷新用户的空间列表.

    在 commit 前序列化实体, 避免 expire_on_commit 触发懒加载。
    对于隔离且未发布的 Runtime, 不提供主机直连访问 URL。
    """
    runtimes = [runtime for _, runtime in rows if runtime is not None]
    snapshot = (
        _status_snapshot(user_id)
        if any(runtime.container_id for runtime in runtimes)
        else None
    )
    changes = _ListChanges(status={}, container={}, missing=[])
    result = [
        _list_item(workspace, runtime, user_id, snapshot, changes)
        for workspace, runtime in rows
    ]
    _apply_status_changes(session, runtimes, changes)
    return result


def _active_operation(
    session: Session,
    runtime: OpenCodeRuntime | None,
) -> RuntimeOperation | None:
    """返回该运行时当前活跃的代际标识 (若存在)."""
    if runtime is None:
        return None
    return (
        session.query(RuntimeOperation)
        .filter(
            RuntimeOperation.runtime_id == runtime.id,
            RuntimeOperation.state.in_(["preparing", "switched"]),
        )
        .populate_existing()
        .first()
    )


def _stale_attempt(
    operation: RuntimeOperation | None,
    attempt: StartAttempt,
) -> bool:
    """恢复流程是否已不再持有当前代际的所有权."""
    return attempt.expected_generation is not None and (
        operation is None or operation.generation != attempt.expected_generation
    )


def _select_existing_container(
    session: Session,
    runtime: OpenCodeRuntime | None,
) -> ContainerPayload | None:
    """返回已满足当前代际身份要求的活跃容器记录."""
    if runtime is None or not runtime.container_id:
        return None
    try:
        existing = _client().inspect(runtime.container_id)
    except ControllerNotFound:
        return None
    cancelled = (
        session.query(RuntimeOperation.generation)
        .filter(
            RuntimeOperation.runtime_id == runtime.id,
            RuntimeOperation.candidate_container_id == existing["id"],
            RuntimeOperation.state == "cancelled",
        )
        .first()
    )
    if cancelled is not None or not _container_uses_configured_image(existing):
        return None
    return existing


def _retire_generation(
    workspace_id: int,
    user_id: int,
    runtime_id: int,
    generation: str,
) -> None:
    """退役数据库不再准入的已准备好的代际资源.

    失败将递延给后台 Worker, 由其负责重试已取消的操作。
    """
    try:
        _client().retire_generation(workspace_id, user_id, runtime_id, generation)
    except Exception as exc:
        logger.error(
            "Runtime generation retirement deferred: error_type={}",
            type(exc).__name__,
        )


def _prepare_generation(
    session: Session,
    workspace_id: int,
    user_id: int,
    operation: RuntimeOperation,
) -> tuple[OpenCodeRuntime, str]:
    """准备已准入的单代际并在数据库中登记其候选身份."""
    generation = operation.generation
    runtime_id = int(operation.runtime_id)
    if operation.state == "preparing":
        prepared = _client().prepare(workspace_id, user_id, runtime_id, generation)
        try:
            runtime_operations_applied = operations.record_candidate(
                session,
                generation,
                prepared["id"],
            )
            session.commit()  # 即使发布失败也保留候选代际记录。
            runtime = operations.switch_candidate(session, generation)
            session.commit()  # 原子性执行旧资源清理与新代际身份发布。
        except ConflictError:
            session.rollback()
            # 已准入代际已失效 (编辑权限被吊销或被更新操作取代)。
            # 立即移除其代际资源而无需等待后台对账,
            # 随后向上层上报调用方当前的权限状态。
            _retire_generation(workspace_id, user_id, runtime_id, generation)
            operations.runtime_with_write_access(session, workspace_id, user_id)
            raise
        return (
            runtime,
            runtime_operations_applied.candidate_container_id or prepared["id"],
        )
    candidate_id = operation.candidate_container_id
    runtime = operations.switch_candidate(session, generation)
    session.commit()  # 原子性执行旧资源清理与新代际身份发布。
    if candidate_id is None:
        # switch_candidate 已强制要求存在记录在案的候选代际。
        message = "Runtime candidate has not been recorded"
        raise ConflictError(message)
    return runtime, candidate_id


def _admit_and_prepare(
    session: Session,
    workspace: Workspace,
    state: StartState,
    attempt: StartAttempt,
) -> tuple[OpenCodeRuntime, ContainerPayload] | None:
    """准入、准备并检查单个代际; 返回 None 表示已排队重试替换."""
    operation = operations.admit(session, state.workspace_id, state.user_id)
    state.generation = operation.generation
    state.runtime = None
    session.commit()  # 在每次 prepare RPC 发起前, 代际状态均可恢复。
    try:
        runtime, container_id = _prepare_generation(
            session,
            state.workspace_id,
            state.user_id,
            operation,
        )
    except ControllerGenerationMissing as error:
        _replace_missing(
            session,
            workspace,
            state,
            CandidateReplacement(
                generation=operation.generation,
                container_id=error.container_id,
                attempt=attempt.attempt,
            ),
        )
        return None
    state.runtime = runtime
    try:
        container = _client().inspect(container_id)
    except ControllerNotFound:
        _replace_missing(
            session,
            workspace,
            state,
            CandidateReplacement(
                generation=operation.generation,
                container_id=container_id,
                attempt=attempt.attempt,
            ),
        )
        return None
    return runtime, container


def _replace_missing(
    session: Session,
    workspace: Workspace,
    state: StartState,
    candidate: CandidateReplacement,
) -> None:
    """替换已消失的候选代际并重新发起启动尝试."""
    if candidate.attempt >= 1:
        message = "Repeated candidate disappearance; retry later"
        raise ControllerError(message)
    operation = operations.replace_missing(
        session,
        candidate.generation,
        candidate.container_id,
    )
    replacement = operation.generation
    runtime_id = int(operation.runtime_id)
    session.commit()  # 旧操作取消与新意图登记在退役失败时依然有效保留。
    _client().retire_generation(
        state.workspace_id,
        state.user_id,
        runtime_id,
        candidate.generation,
    )
    start(
        session,
        workspace,
        state.user_id,
        expected_generation=replacement,
        _replacement_attempt=candidate.attempt + 1,
    )


def _start_published(
    session: Session,
    workspace: Workspace,
    state: StartState,
    runtime: OpenCodeRuntime,
    container: ContainerPayload,
) -> None:
    """启动已发布的容器并完成其代际簿记状态更新."""
    runtime = operations.current_runtime(
        session,
        operations.CurrentRuntimeParams(
            workspace_id=state.workspace_id,
            user_id=state.user_id,
            runtime_id=int(runtime.id),
            container_id=container["id"],
            generation=state.generation,
        ),
    )
    _client().action(container["id"], "start")
    runtime.status = "running"
    runtime.error_message = None
    runtime.last_started_at = utc_now()
    runtime.last_used_at = utc_now()
    session.commit()
    _apply_runtime_config(
        session,
        workspace,
        state.user_id,
        RuntimeProvisioning(
            runtime=runtime,
            reload_config=state.generation is not None,
            generation=state.generation,
        ),
    )
    if state.generation is not None:
        operations.complete(session, state.generation)
        session.commit()


def _activate(
    session: Session,
    workspace: Workspace,
    state: StartState,
    attempt: StartAttempt,
) -> None:
    """准备、发布并启动单个代际, 或在检测到请求陈旧时执行停止."""
    operation = _active_operation(session, state.runtime)
    if _stale_attempt(operation, attempt):
        return
    container = (
        None
        if operation is not None
        else _select_existing_container(session, state.runtime)
    )
    if container is None:
        prepared = _admit_and_prepare(session, workspace, state, attempt)
        if prepared is None:
            return
        runtime, container = prepared
        _start_published(session, workspace, state, runtime, container)
        return
    existing_runtime = state.runtime
    if existing_runtime is None:  # 理论不可达: 容器严格归属于当前运行时。
        return
    _start_published(session, workspace, state, existing_runtime, container)


def _record_start_failure(
    session: Session,
    state: StartState,
    error: ControllerError,
) -> None:
    """记录单次失败的启动操作, 不会重建已删除的运行时."""
    session.rollback()
    # 记录 RPC 失败时绝不重新创建已被删除的所有者或运行时。
    if state.generation is not None:
        runtime_operations.defer_operation(session, state.generation, error)
    else:
        runtime = operations.runtime_with_write_access(
            session,
            state.workspace_id,
            state.user_id,
        )
        if runtime is not None:
            runtime.status = "error"
            runtime.error_message = _runtime_error_message(error)
            session.commit()
    logger.error(
        "Runtime start failed: workspace_id={}, user_id={}, error_type={}",
        state.workspace_id,
        state.user_id,
        type(error).__name__,
    )


@serialized
def start(
    session: Session,
    workspace: Workspace,
    user_id: int,
    *,
    expected_generation: str | None = None,
    _replacement_attempt: int = 0,
) -> Workspace:
    """在发布其 SQL 身份前先准备好持久化的代际资源."""
    state = StartState(
        workspace_id=int(workspace.id),
        user_id=user_id,
        runtime=operations.runtime_with_write_access(
            session,
            int(workspace.id),
            user_id,
        ),
    )
    try:
        _activate(
            session,
            workspace,
            state,
            StartAttempt(
                expected_generation=expected_generation,
                attempt=_replacement_attempt,
            ),
        )
    except ControllerError as error:
        _record_start_failure(session, state, error)
    return workspace


def _stop_legacy(session: Session, workspace: Workspace) -> Workspace:
    """停止旧版工作空间级别的无独立属主容器."""
    try:
        if workspace.container_id:
            _client().action(workspace.container_id, "stop")
        workspace.status = "stopped"
        workspace.error_message = None
    except ControllerNotFound:
        workspace.container_id = None
        workspace.status = "stopped"
    except ControllerError as exc:
        workspace.status = "error"
        workspace.error_message = _runtime_error_message(exc)
        logger.error(
            "停止 OpenCode 容器失败: workspace_id={}, error_type={}",
            workspace.id,
            type(exc).__name__,
        )
    session.commit()
    return workspace


@serialized
def _stop_runtime(session: Session, workspace: Workspace, user_id: int) -> Workspace:
    """在取消活跃代际后停止调用方的运行时."""
    runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if not runtime:
        return workspace
    runtime = operations.runtime_with_write_access(
        session,
        int(workspace.id),
        user_id,
    )
    if runtime is None:
        return workspace
    if operations.cancel_runtime(session, int(runtime.id)):
        # 在调用停止 RPC 前提交取消操作: 恢复流程绝不能恢复
        # 用户要求停止的代际, 即使当前进程随后发生崩溃。
        session.commit()
        runtime = operations.runtime_with_write_access(
            session,
            int(workspace.id),
            user_id,
        )
        if runtime is None:
            return workspace
    try:
        container_id = runtime.container_id
        if container_id:
            _client().action(container_id, "stop")
        runtime.status = "stopped"
        runtime.error_message = None
        runtime.last_used_at = utc_now()
    except ControllerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
    except ControllerError as exc:
        runtime.status = "error"
        runtime.error_message = _runtime_error_message(exc)
        logger.error(
            "停止 OpenCode Runtime 失败: workspace_id={}, user_id={}, error_type={}",
            workspace.id,
            user_id,
            type(exc).__name__,
        )
    runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
    session.commit()
    return workspace


def stop(
    session: Session,
    workspace: Workspace,
    user_id: int | None = None,
) -> Workspace:
    """停止单个运行时并立即吊销其访问令牌."""
    if user_id is None:
        # 兼容无 runtime 所有者的旧版内部调用方。
        return _stop_legacy(session, workspace)
    return _stop_runtime(session, workspace, user_id)


@serialized
def restart(session: Session, workspace: Workspace, user_id: int) -> Workspace:
    """重启调用方的运行时, 当镜像发生变更时重新编排构建."""
    runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if not runtime or not runtime.container_id:
        return start(session, workspace, user_id)
    runtime = operations.runtime_with_write_access(
        session,
        int(workspace.id),
        user_id,
    )
    if runtime is None:
        # 锁定的 runtime 记录已消失; 无任何实体需要重启。
        return workspace
    active = (
        session.query(RuntimeOperation)
        .filter(
            RuntimeOperation.runtime_id == runtime.id,
            (
                RuntimeOperation.state.in_(["preparing", "switched"])
                | (
                    (RuntimeOperation.state == "cancelled")
                    & (
                        RuntimeOperation.candidate_container_id
                        == runtime.container_id
                    )
                )
            ),
        )
        .first()
    )
    if active is not None:
        return start(session, workspace, user_id)
    return _restart_existing(session, workspace, user_id, runtime)


def _restart_existing(
    session: Session,
    workspace: Workspace,
    user_id: int,
    runtime: OpenCodeRuntime,
) -> Workspace:
    """重启单个已发布的容器, 无法直接重启时重新编排构建."""
    try:
        container_id = runtime.container_id
        if container_id is None:
            return start(session, workspace, user_id)
        container = _client().inspect(container_id)
        if not _container_uses_configured_image(container):
            return start(session, workspace, user_id)
        _client().action(container_id, "restart")
        runtime.status = "running"
        runtime.error_message = None
        runtime.last_started_at = utc_now()
        runtime.last_used_at = utc_now()
        session.commit()
        setup_mcp(session, workspace, user_id, runtime=runtime)
    except ControllerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
        return start(session, workspace, user_id)
    except ControllerError as exc:
        runtime.status = "error"
        runtime.error_message = _runtime_error_message(exc)
        session.commit()
        logger.error(
            "重启 OpenCode Runtime 失败: workspace_id={}, user_id={}, error_type={}",
            workspace.id,
            user_id,
            type(exc).__name__,
        )
    return workspace


def remove_container(workspace: Workspace, session: Session | None = None) -> None:
    """原子性解绑关联资源; 后台 Worker 负责清理已提交的身份."""
    if session is not None:
        for runtime in runtime_service.list_runtimes(session, int(workspace.id)):
            remove_runtime(runtime, session)
        stage_container_deletion_fallback(session, workspace.container_id)
        workspace.container_id = None
        workspace.status = "stopped"
        workspace.error_message = None
        session.flush()
        return
    # 显式的非事务性调用方保持原有的同步 RPC 契约行为。
    container_id = workspace.container_id
    if container_id:
        _remove_container_now(container_id)


def _remove_container_now(container_id: str) -> None:
    with suppress(ControllerNotFound):
        _client().action(container_id, "remove")


def remove_runtime(runtime: OpenCodeRuntime, session: Session | None = None) -> None:
    """在调用方事务中吊销访问权限并暂存移除任务."""
    if session is not None:
        operations.cancel_runtime(session, int(runtime.id))
        stage_container_deletion_fallback(session, runtime.container_id)
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        runtime.container_id = None
        runtime.status = "stopped"
        runtime.error_message = None
        runtime.last_used_at = utc_now()
        session.flush()
        return
    container_id = runtime.container_id
    if container_id:
        _remove_container_now(container_id)


def get_access_url(entity: Workspace | OpenCodeRuntime) -> str | None:
    """返回 Runtime 已发布的主机端口 (若存在)."""
    container_id = entity.container_id
    if not container_id:
        return None
    try:
        container = _client().inspect(container_id)
        ports = container["ports"]
        mapping = ports.get("3000/tcp") or []
        if mapping and mapping[0].get("HostPort"):
            return f"http://localhost:{mapping[0]['HostPort']}"
    except ControllerError as exc:
        logger.warning(
            "读取 OpenCode 访问地址失败: entity_id={}, error_type={}",
            getattr(entity, "id", None),
            type(exc).__name__,
        )
    return None


def merge_opencode_mcp_config(
    existing: "JsonObject | None",
    mcp_config: JsonObject,
) -> JsonObject:
    """仅将 SKYCLOUD MCP 配置块合并至现有运行时配置中."""
    return runtime_config.merge_opencode_mcp_config(existing, mcp_config)


def _chat_api_runtime_config(runtime_token: str) -> dict[str, str]:
    """仅向不可信代码暴露网关地址与特定范围内的凭据."""
    configured = get_chat_model_config()
    base_url = os.getenv("OPENCODE_LLM_GATEWAY_URL")
    if not base_url:
        base_url = DEFAULT_LLM_GATEWAY_URL
    return {
        "base_url": base_url.rstrip("/"),
        "model": configured["model"].strip(),
        "api_key": runtime_token,
    }


def _model_config_policy() -> ModelConfigPolicy:
    return ModelConfigPolicy(
        provider_id=OPENCODE_CHAT_PROVIDER_ID,
        output_limit=runtime_llm_tokens.positive_setting(
            "RUNTIME_LLM_MAX_OUTPUT_TOKENS",
            4096,
        ),
        context_limit=runtime_llm_tokens.positive_setting(
            "RUNTIME_LLM_CONTEXT_TOKENS",
            65536,
        ),
    )


def merge_opencode_chat_provider(
    existing: "JsonObject | None",
    chat_config: dict[str, str] | None = None,
) -> JsonObject:
    """将作用域受限的 Chat 供应商配置合并至现有 OpenCode 配置中."""
    return runtime_config.merge_opencode_chat_provider(
        existing,
        chat_config,
        **_model_config_policy(),
    )


def opencode_chat_provider_needs_reload(
    existing: JsonObject,
    chat_config: dict[str, str],
) -> bool:
    """运行中的 Runtime 供应商配置块是否与该目标配置存在差异."""
    return runtime_config.opencode_chat_provider_needs_reload(
        existing,
        chat_config,
        **_model_config_policy(),
    )


def merge_opencode_expert_agent(existing: "JsonObject | None") -> JsonObject:
    """将最小权限专家 Agent 配置合并至现有配置中."""
    return runtime_config.merge_opencode_expert_agent(
        existing,
        agent_name=OPENCODE_EXPERT_AGENT,
    )


def _read_opencode_config(container_id: str) -> JsonObject:
    return _client().read_config(container_id)


def _write_opencode_config(container_id: str, config: JsonObject) -> None:
    _client().write_config(container_id, config)


def setup_mcp(
    session: Session,
    workspace: Workspace,
    user_id: int,
    *,
    runtime: OpenCodeRuntime | None = None,
) -> None:
    """签发作用域受限的运行时令牌并原子性合并其 MCP 配置."""
    _apply_runtime_config(
        session,
        workspace,
        user_id,
        RuntimeProvisioning(runtime=runtime),
    )


@serialized
def _apply_runtime_config(
    session: Session,
    workspace: Workspace,
    user_id: int,
    provisioning: RuntimeProvisioning,
) -> None:
    """将 MCP 与模型供应商配置写入运行中的 Runtime.

    通过实例 API 重新加载配置, 保持有界的 tmpfs 存储容量,
    并将所有配置编排指令限制在 Runtime UID 权限下运行。
    """
    runtime = provisioning.runtime
    if runtime is None:
        runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if runtime is None:
        raise BusinessRuleError(RUNTIME_NOT_CREATED)

    container_id = runtime.container_id
    if runtime.status != "running" or not container_id:
        raise BusinessRuleError(NOT_RUNNING)

    runtime_id = int(runtime.id)
    workspace_id = int(workspace.id)
    published = operations.CurrentRuntimeParams(
        workspace_id=workspace_id,
        user_id=user_id,
        runtime_id=runtime_id,
        container_id=container_id,
        generation=provisioning.generation,
    )
    runtime = operations.current_runtime(session, published)
    role = get_member_role(session, workspace_id, user_id)
    if role is None:
        raise BusinessRuleError(MEMBER_REQUIRED)

    record, token = runtime_token_service.refresh_runtime_token(
        session,
        runtime,
        role=role.value,
    )
    config: JsonObject = {
        "type": "remote",
        "url": _mcp_endpoint(),
        "enabled": True,
        "oauth": False,
        "headers": {"Authorization": f"Bearer {token}"},
    }
    try:
        existing = _read_opencode_config(container_id)
        runtime = operations.current_runtime(session, published)
        llm_token = runtime_llm_tokens.issue_token(session, runtime)
        runtime = operations.current_runtime(session, published)
        chat_config = _chat_api_runtime_config(llm_token)
        provider_reload_needed = opencode_chat_provider_needs_reload(
            existing,
            chat_config,
        )
        merged = merge_opencode_mcp_config(existing, config)
        merged = merge_opencode_chat_provider(merged, chat_config)
        merged = merge_opencode_expert_agent(merged)
        _sync_mcp_via_runtime_api(
            workspace,
            user_id,
            runtime,
            config,
            RuntimeConfigSync(
                reload_config=provisioning.reload_config or provider_reload_needed,
                runtime_config=merged,
            ),
        )
        _write_opencode_config(container_id, merged)
        runtime.config_version = int(runtime.config_version or 0) + 1
        runtime.last_used_at = utc_now()
        session.commit()
        logger.info(
            "已配置 OpenCode MCP: workspace_id={}, user_id={}, runtime_id={}, "
            "token_id={}, config_version={}",
            workspace.id,
            user_id,
            runtime.id,
            record.id,
            runtime.config_version,
        )
    except ControllerError as exc:
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
        logger.error(
            "配置 OpenCode MCP 失败: workspace_id={}, user_id={}, "
            "runtime_id={}, error_type={}",
            workspace.id,
            user_id,
            runtime.id,
            type(exc).__name__,
        )
        raise


