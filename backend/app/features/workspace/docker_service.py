"""OpenCode Docker lifecycle and per-runtime MCP configuration."""

import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TypedDict

import httpx
from loguru import logger
from runtime_controller.client import (
    ControllerClient,
    ControllerGenerationMissing,
    RemoteContainer,
    RemoteContainerConfig,
    runtime_url,
)
from runtime_controller.client import ControllerError as DockerException
from runtime_controller.client import (
    ControllerNotFound as ContainerNotFound,
)
from runtime_controller.client import (
    ControllerNotFound as ImageNotFound,
)
from runtime_controller.client import (
    auth_headers as controller_auth_headers,
)
from runtime_controller.json_types import JsonObject
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

# The glibc runtime builder writes an immutable local ID into private .env.
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
    """Token limits and provider identity of the Runtime chat provider."""

    provider_id: str
    output_limit: int
    context_limit: int


@dataclass(frozen=True, slots=True)
class StartAttempt:
    """Requested generation boundary for one start attempt."""

    expected_generation: str | None = None
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class CandidateReplacement:
    """Identity of a prepared candidate that vanished before publication."""

    generation: str
    container_id: str
    attempt: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeConfigSync:
    """Options for pushing MCP/provider config to a live Runtime."""

    reload_config: bool = False
    runtime_config: "JsonObject | None" = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeProvisioning:
    """Provisioning context of one Runtime configuration write."""

    runtime: OpenCodeRuntime | None = None
    reload_config: bool = False
    generation: str | None = None


@dataclass(slots=True)
class StartState:
    """Mutable progress of one start attempt."""

    workspace_id: int
    user_id: int
    runtime: OpenCodeRuntime | None
    generation: str | None = None


def _client() -> ControllerClient:
    return ControllerClient()


def ensure_runtime_image(client: ControllerClient) -> None:
    """Ensure the configured upstream Runtime image is available locally."""
    try:
        client.images.get(OPENCODE_IMAGE)
    except ImageNotFound:
        logger.info("正在拉取 OpenCode 官方镜像: {}", OPENCODE_IMAGE)
    else:
        return

    try:
        client.images.pull(OPENCODE_IMAGE)
    except DockerException as exc:
        message = f"拉取 OpenCode 官方镜像失败: {OPENCODE_IMAGE}"
        raise DockerException(message) from exc


def _container_uses_configured_image(container: RemoteContainer) -> bool:
    """Whether an existing Runtime was created from the current image ref."""
    try:
        config = container.attrs.get(
            "Config", RemoteContainerConfig(Image=None, Labels=None),
        )
        image_ref = config.get("Image")
    except (AttributeError, TypeError):
        # Lightweight Docker fakes used by callers/tests may not expose attrs.
        return True
    labels = config.get("Labels")
    if isinstance(labels, dict) and labels.get(RUNTIME_POLICY_LABEL) != POLICY_VERSION:
        return False
    return not image_ref or image_ref == OPENCODE_IMAGE


def runtime_uses_configured_image(runtime: OpenCodeRuntime) -> bool:
    """Check whether a persisted Runtime already uses ``OPENCODE_IMAGE``.

    A Runtime created before an image-setting change should be rebuilt on its
    next use, rather than continuing to run a stale project-owned image.
    """
    container_id = runtime.container_id
    if not container_id:
        return False
    try:
        container = _client().containers.get(container_id)
    except ContainerNotFound:
        return False
    except DockerException as exc:
        logger.warning(
            "无法检查 OpenCode Runtime 镜像: runtime_id={}, error_type={}",
            runtime.id,
            type(exc).__name__,
        )
        # Do not destroy a healthy Runtime merely because Docker is
        # temporarily unavailable; the caller will surface its normal error.
        return True
    return _container_uses_configured_image(container)


def get_runtime_base_url(
    workspace: Workspace,
    user_id: int,
    runtime: OpenCodeRuntime | None = None,
) -> str:
    """Return a Runtime URL that works for Docker and hybrid local setups.

    Both containerized and local API processes use the authenticated
    Controller relay. They never need a runtime network interface or port.
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
    """Apply fresh MCP credentials to the live Runtime without a restart."""
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


def runtime_container_name(workspace: Workspace, user_id: int | None = None) -> str:
    """Return a stable container name scoped to workspace *and* user."""
    if user_id is None:
        # Legacy callers may still inspect the old workspace-level name. New
        # lifecycle calls always provide user_id.
        return f"skycloud-workspace-{workspace.id}"
    return f"skycloud-opencode-w{workspace.id}-u{user_id}"


def _sync_status(session: Session, workspace: Workspace) -> None:
    """Best-effort synchronization for legacy workspace container fields."""
    container_id = workspace.container_id
    if not container_id:
        return
    try:
        container = _client().containers.get(container_id)
        status = "running" if container.status == "running" else "stopped"
        if workspace.status != status:
            workspace.status = status
            session.commit()
    except ContainerNotFound:
        workspace.container_id = None
        workspace.status = "stopped"
        session.commit()
    except DockerException as exc:
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
        return  # Observing a stopped candidate is not a user stop request.
    try:
        container = _client().containers.get(container_id)
        status = "running" if container.status == "running" else "stopped"
        if runtime.status != status:
            runtime.status = status
            runtime.last_used_at = utc_now()
            session.commit()
    except ContainerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
        runtime.last_used_at = utc_now()
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
    except DockerException as exc:
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
    """Return the current member's runtime summary.

    The workspace-level fields remain as a read-only compatibility fallback
    for deployments created before ``opencode_runtimes`` existed.
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
    except DockerException as exc:
        logger.warning(
            "批量同步 OpenCode 状态失败: user_id={}, error_type={}",
            user_id,
            type(exc).__name__,
        )
        return None


@dataclass(slots=True)
class _ListChanges:
    """Observed runtime changes awaiting one bulk SQL synchronization."""

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
    # Compare the observed container too: a concurrent recreation must not
    # be overwritten or have its new credentials revoked by an old snapshot.
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
    """Refresh a user's list with constant database and Controller requests.

    Serialize before committing so expire_on_commit cannot cause lazy reloads.
    No host access URL is available for the isolated, unpublished runtimes.
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
    """Return the current active generation of this runtime, if any."""
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
    """Whether a resumed recovery no longer owns the current generation."""
    return attempt.expected_generation is not None and (
        operation is None or operation.generation != attempt.expected_generation
    )


def _select_existing_container(
    session: Session,
    runtime: OpenCodeRuntime | None,
) -> RemoteContainer | None:
    """Return a live container that already satisfies the current identity."""
    if runtime is None or not runtime.container_id:
        return None
    try:
        existing = _client().containers.get(runtime.container_id)
    except ContainerNotFound:
        return None
    cancelled = (
        session.query(RuntimeOperation.generation)
        .filter(
            RuntimeOperation.runtime_id == runtime.id,
            RuntimeOperation.candidate_container_id == existing.id,
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
    """Retire a prepared generation the database no longer admits.

    Failure is deferred to the Worker, which retries cancelled operations.
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
    """Prepare one admitted generation and publish its candidate identity."""
    generation = operation.generation
    runtime_id = int(operation.runtime_id)
    if operation.state == "preparing":
        prepared = _client().prepare(workspace_id, user_id, runtime_id, generation)
        try:
            runtime_operations_applied = operations.record_candidate(
                session,
                generation,
                prepared.id,
            )
            session.commit()  # Preserve candidate even if publication fails.
            runtime = operations.switch_candidate(session, generation)
            session.commit()  # Old cleanup and new identity publish atomically.
        except ConflictError:
            session.rollback()
            # The admitted generation is gone (revoked edit permission or a
            # superseding operation). Remove its resource now instead of waiting
            # for reconciliation, then report the caller's current permission.
            _retire_generation(workspace_id, user_id, runtime_id, generation)
            operations.runtime_with_write_access(session, workspace_id, user_id)
            raise
        return runtime, runtime_operations_applied.candidate_container_id or prepared.id
    candidate_id = operation.candidate_container_id
    runtime = operations.switch_candidate(session, generation)
    session.commit()  # Old cleanup and new identity publish atomically.
    if candidate_id is None:
        # switch_candidate already requires a recorded candidate.
        message = "Runtime candidate has not been recorded"
        raise ConflictError(message)
    return runtime, candidate_id


def _admit_and_prepare(
    session: Session,
    workspace: Workspace,
    state: StartState,
    attempt: StartAttempt,
) -> tuple[OpenCodeRuntime, RemoteContainer] | None:
    """Admit, prepare and inspect one generation; None means replacement queued."""
    operation = operations.admit(session, state.workspace_id, state.user_id)
    state.generation = operation.generation
    state.runtime = None
    session.commit()  # Generation is recoverable before every prepare RPC.
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
        container = _client().containers.get(container_id)
    except ContainerNotFound:
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
    """Replace one vanished candidate generation and restart the attempt."""
    if candidate.attempt >= 1:
        message = "Repeated candidate disappearance; retry later"
        raise DockerException(message)
    operation = operations.replace_missing(
        session,
        candidate.generation,
        candidate.container_id,
    )
    replacement = operation.generation
    runtime_id = int(operation.runtime_id)
    session.commit()  # Old cancellation + new intent survive retirement failure.
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
    container: RemoteContainer,
) -> None:
    """Start the published container and finish its generation bookkeeping."""
    runtime = operations.current_runtime(
        session,
        operations.CurrentRuntimeParams(
            workspace_id=state.workspace_id,
            user_id=state.user_id,
            runtime_id=int(runtime.id),
            container_id=container.id,
            generation=state.generation,
        ),
    )
    container.start()
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
    """Prepare, publish and start one generation, or stop on a stale request."""
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
    if existing_runtime is None:  # unreachable: the container belongs to this runtime
        return
    _start_published(session, workspace, state, existing_runtime, container)


def _record_start_failure(
    session: Session,
    state: StartState,
    error: DockerException,
) -> None:
    """Record one failed start without recreating a deleted runtime."""
    session.rollback()
    # Never recreate a deleted owner/runtime while recording an RPC failure.
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
    """Prepare a durable generation before publishing its SQL identity."""
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
    except DockerException as error:
        _record_start_failure(session, state, error)
    return workspace


def _stop_legacy(session: Session, workspace: Workspace) -> Workspace:
    """Stop the legacy workspace-level container without runtime ownership."""
    try:
        if workspace.container_id:
            _client().containers.get(workspace.container_id).stop()
        workspace.status = "stopped"
        workspace.error_message = None
    except ContainerNotFound:
        workspace.container_id = None
        workspace.status = "stopped"
    except DockerException as exc:
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
    """Stop the caller's runtime after cancelling its active generations."""
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
        # Commit the cancellation before the stop RPC: recovery must never resume a
        # generation the user asked to stop, even if this process dies afterwards.
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
            _client().containers.get(container_id).stop()
        runtime.status = "stopped"
        runtime.error_message = None
        runtime.last_used_at = utc_now()
    except ContainerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
    except DockerException as exc:
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
    """Stop one runtime and revoke its token immediately."""
    if user_id is None:
        # Compatibility for old internal callers that have no runtime owner.
        return _stop_legacy(session, workspace)
    return _stop_runtime(session, workspace, user_id)


@serialized
def restart(session: Session, workspace: Workspace, user_id: int) -> Workspace:
    """Restart the caller's runtime, reprovisioning when its image changed."""
    runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if not runtime or not runtime.container_id:
        return start(session, workspace, user_id)
    runtime = operations.runtime_with_write_access(
        session,
        int(workspace.id),
        user_id,
    )
    if runtime is None:
        # The locked runtime row vanished; nothing remains to restart.
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
    """Restart one published container, reprovisioning when it cannot."""
    try:
        container_id = runtime.container_id
        if container_id is None:
            return start(session, workspace, user_id)
        container = _client().containers.get(container_id)
        if not _container_uses_configured_image(container):
            return start(session, workspace, user_id)
        container.restart()
        runtime.status = "running"
        runtime.error_message = None
        runtime.last_started_at = utc_now()
        runtime.last_used_at = utc_now()
        session.commit()
        setup_mcp(session, workspace, user_id, runtime=runtime)
    except ContainerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
        return start(session, workspace, user_id)
    except DockerException as exc:
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
    """Detach resources atomically; the Worker removes committed identities."""
    if session is not None:
        for runtime in runtime_service.list_runtimes(session, int(workspace.id)):
            remove_runtime(runtime, session)
        stage_container_deletion_fallback(session, workspace.container_id)
        workspace.container_id = None
        workspace.status = "stopped"
        workspace.error_message = None
        session.flush()
        return
    # Explicit nontransactional callers retain the synchronous RPC contract.
    container_id = workspace.container_id
    if container_id:
        _remove_container_now(container_id)


def _remove_container_now(container_id: str) -> None:
    with suppress(ContainerNotFound):
        _client().containers.get(container_id).remove()


def remove_runtime(runtime: OpenCodeRuntime, session: Session | None = None) -> None:
    """Revoke access and stage removal in the caller's transaction."""
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
    """Return the published host port of a Runtime, when one exists."""
    container_id = entity.container_id
    if not container_id:
        return None
    try:
        container = _client().containers.get(container_id)
        ports = container.attrs["NetworkSettings"]["Ports"]
        mapping = ports.get("3000/tcp") or []
        if mapping and mapping[0].get("HostPort"):
            return f"http://localhost:{mapping[0]['HostPort']}"
    except DockerException as exc:
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
    """Merge only the SKYCLOUD MCP block into the existing config."""
    return runtime_config.merge_opencode_mcp_config(existing, mcp_config)


def _chat_api_runtime_config(runtime_token: str) -> dict[str, str]:
    """Expose only the gateway and scoped credential to untrusted code."""
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
    """Merge the scoped chat provider into the existing OpenCode config."""
    return runtime_config.merge_opencode_chat_provider(
        existing,
        chat_config,
        **_model_config_policy(),
    )


def opencode_chat_provider_needs_reload(
    existing: JsonObject,
    chat_config: dict[str, str],
) -> bool:
    """Whether the live Runtime provider block differs from this config."""
    return runtime_config.opencode_chat_provider_needs_reload(
        existing,
        chat_config,
        **_model_config_policy(),
    )


def merge_opencode_expert_agent(existing: "JsonObject | None") -> JsonObject:
    """Merge the least-privilege expert agent into the existing config."""
    return runtime_config.merge_opencode_expert_agent(
        existing,
        agent_name=OPENCODE_EXPERT_AGENT,
    )


def _read_opencode_config(container: RemoteContainer) -> JsonObject:
    return container.read_config()


def _write_opencode_config(container: RemoteContainer, config: JsonObject) -> None:
    container.write_config(config)


def setup_mcp(
    session: Session,
    workspace: Workspace,
    user_id: int,
    *,
    runtime: OpenCodeRuntime | None = None,
) -> None:
    """Issue a scoped runtime token and atomically merge its MCP config."""
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
    """Write MCP and model provider config to one running Runtime.

    Configuration is reloaded through the instance API, preserving bounded
    tmpfs storage and keeping all provisioning commands under the runtime UID.
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
        container = _client().containers.get(container_id)
        existing = _read_opencode_config(container)
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
        _write_opencode_config(container, merged)
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
    except DockerException as exc:
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


def create_runtime_container(
    workspace: Workspace,
    user_id: int,
    runtime_id: int | None = None,
) -> RemoteContainer:
    """Create the container for one prepared runtime generation."""
    if not runtime_id:
        message = "Runtime identity is required"
        raise BusinessRuleError(message)
    # Policy and Docker access exist only in the restricted controller.
    container = _client().create(int(workspace.id), int(user_id), int(runtime_id))
    logger.info(
        "Runtime Controller 已创建容器: runtime_id={}, container={}",
        runtime_id,
        container.short_id,
    )
    return container
