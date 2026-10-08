"""OpenCode Docker lifecycle and per-runtime MCP configuration."""

import os

import httpx
from loguru import logger
from sqlalchemy import case, update, exists, select
from sqlalchemy.orm import Session
from app.models.mcp_runtime_token import McpRuntimeToken

from app.exceptions import BusinessRuleError
from app.features.workspace import (runtime_service, runtime_config, runtime_config_client,
                                    runtime_operation_repository as operations)
from app.features.workspace.runtime_execution import serialized
from app.models.runtime_operation import RuntimeOperation
from app.features.workspace.permissions import get_member_role
from app.features.assistant.clients.opencode_auth import server_password, server_username
from app.infra.datetime_utils import utc_now
from app.infra.llm.config import get_chat_model_config
from app.features.runtime_llm import tokens as runtime_llm_tokens
from app.mcp import runtime_token_service
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.workspace import Workspace


from runtime_controller.client import (
    ControllerClient, ControllerError as DockerException,
    ControllerNotFound as ContainerNotFound, ControllerNotFound as ImageNotFound,
    ControllerGenerationMissing,
    auth_headers as controller_auth_headers, runtime_url,
)
from runtime_controller.policy import POLICY_VERSION


# The glibc runtime builder writes an immutable local ID into private .env.
OPENCODE_IMAGE = os.getenv("OPENCODE_IMAGE", "")
MCP_PORT = int(os.getenv("MCP_PORT", "5001"))
OPENCODE_EXPERT_AGENT = os.getenv("OPENCODE_EXPERT_AGENT", "skycloud-expert")
OPENCODE_CHAT_PROVIDER_ID = os.getenv("OPENCODE_CHAT_PROVIDER_ID", "skycloud-chat")


def _client() -> ControllerClient:
    return ControllerClient()


def _ensure_opencode_image(client: ControllerClient) -> None:
    """Ensure the configured upstream Runtime image is available locally."""

    try:
        client.images.get(OPENCODE_IMAGE)
        return
    except ImageNotFound:
        logger.info("正在拉取 OpenCode 官方镜像：{}", OPENCODE_IMAGE)

    try:
        client.images.pull(OPENCODE_IMAGE)
    except DockerException as exc:
        raise DockerException(f"拉取 OpenCode 官方镜像失败：{OPENCODE_IMAGE}") from exc


def _container_uses_configured_image(container) -> bool:
    """Whether an existing Runtime was created from the current image ref."""

    try:
        image_ref = container.attrs.get("Config", {}).get("Image")
    except (AttributeError, TypeError):
        # Lightweight Docker fakes used by callers/tests may not expose attrs.
        return True
    labels = container.attrs.get("Config", {}).get("Labels")
    if isinstance(labels, dict) and labels.get("skycloud.runtime_policy") != POLICY_VERSION:
        return False
    return not image_ref or image_ref == OPENCODE_IMAGE


def runtime_uses_configured_image(runtime: OpenCodeRuntime) -> bool:
    """Check whether a persisted Runtime already uses ``OPENCODE_IMAGE``.

    A Runtime created before an image-setting change should be rebuilt on its
    next use, rather than continuing to run a stale project-owned image.
    """

    if not runtime.container_id:
        return False
    try:
        container = _client().containers.get(runtime.container_id)
    except ContainerNotFound:
        return False
    except DockerException as exc:
        logger.warning(
            "无法检查 OpenCode Runtime 镜像：runtime_id={}, error_type={}",
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

    configured = os.getenv("OPENCODE_RUNTIME_BASE_URL") or os.getenv("OPENCODE_SERVER_URL")
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
    raise BusinessRuleError("Runtime has not been created")


def _mcp_endpoint() -> str:
    configured = os.getenv("OPENCODE_MCP_URL")
    if configured:
        return configured.rstrip("/")
    return f"http://skycloud-backend-mcp:{MCP_PORT}/mcp"


def _startup_timeout_seconds() -> float:
    try:
        return max(float(os.getenv("OPENCODE_STARTUP_TIMEOUT", "30")), 1.0)
    except ValueError:
        return 30.0


def _sync_mcp_via_runtime_api(
    workspace: Workspace,
    user_id: int,
    runtime: OpenCodeRuntime,
    config: dict,
    *,
    reload_config: bool = False,
    runtime_config: dict | None = None,
) -> None:
    """Apply fresh MCP credentials to the live Runtime without a restart."""

    base_url = get_runtime_base_url(workspace, user_id, runtime)
    auth = httpx.BasicAuth(
        server_username(),
        server_password(int(runtime.id), int(user_id), int(workspace.id)),
    )
    runtime_config_client.sync_mcp(
        base_url, auth, controller_auth_headers(base_url), config,
        startup_timeout=_startup_timeout_seconds(), reload_config=reload_config,
        runtime_config=runtime_config,
    )


def _name(workspace: Workspace, user_id: int | None = None) -> str:
    """Return a stable container name scoped to workspace *and* user."""

    if user_id is None:
        # Legacy callers may still inspect the old workspace-level name. New
        # lifecycle calls always provide user_id.
        return f"skycloud-workspace-{workspace.id}"
    return f"skycloud-opencode-w{workspace.id}-u{user_id}"


def _sync_status(session: Session, workspace: Workspace) -> None:
    """Best-effort synchronization for legacy workspace container fields."""

    if not workspace.container_id:
        return
    try:
        container = _client().containers.get(workspace.container_id)
        status = "running" if container.status == "running" else "stopped"
        if workspace.status != status:
            workspace.status = status
            session.commit()
    except ContainerNotFound:
        workspace.container_id = None
        workspace.status = "stopped"
        session.commit()
    except DockerException as exc:
        logger.warning("同步 OpenCode 容器状态失败：workspace_id={}, error_type={}", workspace.id, type(exc).__name__)


def _sync_runtime_status(session: Session, runtime: OpenCodeRuntime) -> None:
    if not runtime.container_id:
        return
    if session.query(RuntimeOperation.generation).filter(RuntimeOperation.runtime_id == runtime.id,
            RuntimeOperation.state.in_(['preparing','switched'])).first() is not None:
        return  # Observing a stopped candidate is not a user stop request.
    try:
        container = _client().containers.get(runtime.container_id)
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
        logger.warning("同步 OpenCode Runtime 状态失败：runtime_id={}, error_type={}", runtime.id, type(exc).__name__)


def _runtime_summary(session: Session, runtime: OpenCodeRuntime) -> dict:
    _sync_runtime_status(session, runtime)
    return {
        "runtime_id": runtime.id,
        "user_id": runtime.user_id,
        "container_id": runtime.container_id[:12] if runtime.container_id else None,
        "status": runtime.status,
        "error_message": runtime.error_message,
        "config_version": runtime.config_version,
        "access_url": (
            get_access_url(runtime) if runtime.status == "running" else None
        ),
    }


def _runtime_error_message(exc: Exception) -> str:
    message = str(exc)
    if "No such image" in message or "pull access denied" in message:
        return (
            f"无法获取 OpenCode 官方镜像（{OPENCODE_IMAGE}）。"
            f"请检查 Docker Desktop 是否能访问 ghcr.io，或手动执行：docker pull {OPENCODE_IMAGE}"
        )
    return "Runtime 操作失败，请检查服务器日志"


def summary(
        session: Session,
        workspace: Workspace,
        user_id: int | None = None,
) -> dict:
    """Return the current member's runtime summary.

    The workspace-level fields remain as a read-only compatibility fallback
    for deployments created before ``opencode_runtimes`` existed.
    """

    if user_id is not None:
        runtime = runtime_service.get_runtime(session, int(workspace.id), int(user_id))
        if runtime:
            return _runtime_summary(session, runtime)

    _sync_status(session, workspace)
    return {
        "runtime_id": None,
        "user_id": user_id,
        "container_id": workspace.container_id[:12] if workspace.container_id else None,
        "status": workspace.status,
        "error_message": workspace.error_message,
        "config_version": None,
        "access_url": get_access_url(workspace) if workspace.status == "running" else None,
    }


def list_summaries(session: Session, rows: list, user_id: int) -> list[dict]:
    """Refresh a user's list with constant database and Controller requests.

    Serialize before committing so expire_on_commit cannot cause lazy reloads.
    No host access URL is available for the isolated, unpublished runtimes.
    """
    runtimes = [runtime for _, runtime in rows if runtime is not None]
    snapshot = None
    if any(runtime.container_id for runtime in runtimes):
        try:
            snapshot = {(row["runtime_id"], row["workspace_id"], row["id"]): row["status"]
                        for row in _client().user_snapshot(user_id)
                        if row["user_id"] == user_id}
        except DockerException as exc:
            logger.warning("批量同步 OpenCode 状态失败：user_id={}, error_type={}", user_id, type(exc).__name__)
    changed_status, changed_container, missing = {}, {}, []
    result = []
    for workspace, runtime in rows:
        if runtime is None:
            result.append({**workspace, "runtime_id": None, "user_id": user_id,
                           "config_version": None, "access_url": None})
            continue
        status, container_id = runtime.status, runtime.container_id
        if snapshot is not None and container_id:
            key = (runtime.id, runtime.workspace_id, container_id)
            status = snapshot.get(key, "stopped")
            if key not in snapshot:
                container_id = None
                missing.append(runtime.id)
            if status != runtime.status or container_id != runtime.container_id:
                changed_status[runtime.id] = status
                changed_container[runtime.id] = container_id
        result.append({**workspace, "runtime_id": runtime.id, "user_id": user_id,
                       "container_id": container_id[:12] if container_id else None,
                       "status": status, "error_message": runtime.error_message,
                       "config_version": runtime.config_version, "access_url": None})
    if changed_status:
        now = utc_now()
        # Compare the observed container too: a concurrent recreation must not
        # be overwritten or have its new credentials revoked by an old snapshot.
        observed = {runtime.id: runtime for runtime in runtimes if runtime.id in changed_status}
        updated = session.execute(
            update(OpenCodeRuntime).where(
                OpenCodeRuntime.id.in_(observed),
                ~exists(select(RuntimeOperation.generation).where(
                    RuntimeOperation.runtime_id == OpenCodeRuntime.id,
                    RuntimeOperation.state.in_(['preparing','switched']))),
                OpenCodeRuntime.container_id == case(
                    {key: runtime.container_id for key, runtime in observed.items()}, value=OpenCodeRuntime.id),
                OpenCodeRuntime.status == case(
                    {key: runtime.status for key, runtime in observed.items()}, value=OpenCodeRuntime.id),
                OpenCodeRuntime.updated_at == case(
                    {key: runtime.updated_at for key, runtime in observed.items()}, value=OpenCodeRuntime.id),
            )
            .values(status=case(changed_status, value=OpenCodeRuntime.id),
                    container_id=case(changed_container, value=OpenCodeRuntime.id),
                    last_used_at=now)
            .returning(OpenCodeRuntime.id),
            execution_options={"synchronize_session": False},
        ).scalars().all()
        removed = set(updated).intersection(missing)
        if removed:
            session.execute(update(McpRuntimeToken).where(
                McpRuntimeToken.runtime_id.in_(removed), McpRuntimeToken.revoked_at.is_(None)
            ).values(revoked_at=now), execution_options={"synchronize_session": False})
        session.commit()
    return result


@serialized
def start(session: Session, workspace: Workspace, user_id: int, *, expected_generation=None, _replacement_attempt=0) -> Workspace:
    """Prepare a durable generation before publishing its SQL identity."""
    workspace_id = int(workspace.id)
    generation = None
    runtime = operations._runtime_with_write_access(session,workspace_id,user_id)
    try:
        operation = None if runtime is None else session.query(RuntimeOperation).filter(
            RuntimeOperation.runtime_id == runtime.id, RuntimeOperation.state.in_(['preparing','switched'])
        ).populate_existing().first()
        if expected_generation is not None and (operation is None or operation.generation != expected_generation):
            return workspace
        container = None
        if operation is None and runtime is not None and runtime.container_id:
            try:
                existing = _client().containers.get(runtime.container_id)
                cancelled = session.query(RuntimeOperation.generation).filter(RuntimeOperation.runtime_id == runtime.id,
                    RuntimeOperation.candidate_container_id == existing.id,RuntimeOperation.state == 'cancelled').first()
                if cancelled is None and _container_uses_configured_image(existing):
                    container = existing
            except ContainerNotFound:
                pass
        if container is None:
            operation = operations.admit(session,workspace_id,user_id)
            generation = operation.generation
            runtime_id = int(operation.runtime_id)
            state = operation.state
            session.commit()  # Generation is recoverable before every prepare RPC.
            if state == 'preparing':
                try:
                    container = _client().prepare(workspace_id,user_id,runtime_id,generation)
                except ControllerGenerationMissing as error:
                    return _replace_missing(session,workspace,user_id,generation,error.container_id,_replacement_attempt)
                operations.record_candidate(session,generation,container.id)
                session.commit()  # Preserve candidate even if publication fails.
            runtime = operations.switch_candidate(session,generation)
            container_id = runtime.container_id
            session.commit()  # Old cleanup and new identity publish atomically.
            try:
                container = _client().containers.get(container_id)
            except ContainerNotFound:
                return _replace_missing(session,workspace,user_id,generation,container_id,_replacement_attempt)
        runtime_id, container_id = int(runtime.id), container.id
        runtime = operations.current_runtime(session,workspace_id,user_id,runtime_id,container_id,generation)
        container.start()
        runtime.status = 'running'
        runtime.error_message = None
        runtime.last_started_at = utc_now()
        runtime.last_used_at = utc_now()
        session.commit()
        setup_mcp(session,workspace,user_id,runtime=runtime,reload_runtime_config=generation is not None,
                  generation=generation)
        if generation is not None:
            operations.complete(session,generation)
            session.commit()
    except DockerException as exc:
        session.rollback()
        # Never recreate a deleted owner/runtime while recording an RPC failure.
        if generation is not None:
            from app.infra.runtime_operations import defer_operation
            defer_operation(session,generation,exc)
        else:
            runtime = operations._runtime_with_write_access(session,workspace_id,user_id)
            if runtime is not None:
                runtime.status = 'error'
                runtime.error_message = _runtime_error_message(exc)
                session.commit()
        logger.error('Runtime start failed: workspace_id={}, user_id={}, error_type={}',workspace_id,user_id,type(exc).__name__)
    return workspace


def _replace_missing(session, workspace, user_id, generation, container_id, attempt):
    if attempt >= 1:
        raise DockerException('Repeated candidate disappearance; retry later')
    operation = operations.replace_missing(session,generation,container_id)
    replacement = operation.generation
    runtime_id = int(operation.runtime_id)
    workspace_id = int(workspace.id)
    session.commit()  # Old cancellation + new intent survive retirement failure.
    _client().retire_generation(workspace_id,user_id,runtime_id,generation)
    return start(session,workspace,user_id,expected_generation=replacement,_replacement_attempt=attempt+1)


@serialized
def stop(
        session: Session,
        workspace: Workspace,
        user_id: int | None = None,
) -> Workspace:
    """Stop one runtime and revoke its token immediately."""

    if user_id is None:
        # Compatibility for old internal callers that have no runtime owner.
        try:
            if workspace.container_id:
                _client().containers.get(workspace.container_id).stop(timeout=10)
            workspace.status = "stopped"
            workspace.error_message = None
        except ContainerNotFound:
            workspace.container_id = None
            workspace.status = "stopped"
        except DockerException as exc:
            workspace.status = "error"
            workspace.error_message = _runtime_error_message(exc)
            logger.error("停止 OpenCode 容器失败：workspace_id={}, error_type={}", workspace.id, type(exc).__name__)
        session.commit()
        return workspace

    runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if not runtime:
        return workspace
    runtime = operations._runtime_with_write_access(session,int(workspace.id),user_id)
    operations.cancel_runtime(session,int(runtime.id))
    try:
        if runtime.container_id:
            _client().containers.get(runtime.container_id).stop(timeout=10)
        runtime.status = "stopped"
        runtime.error_message = None
        runtime.last_used_at = utc_now()
    except ContainerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
    except DockerException as exc:
        runtime.status = "error"
        runtime.error_message = _runtime_error_message(exc)
        logger.error("停止 OpenCode Runtime 失败：workspace_id={}, user_id={}, error_type={}", workspace.id, user_id, type(exc).__name__)
    runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
    session.commit()
    return workspace


@serialized
def restart(session: Session, workspace: Workspace, user_id: int) -> Workspace:
    runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if not runtime or not runtime.container_id:
        return start(session, workspace, user_id)
    runtime = operations._runtime_with_write_access(session,int(workspace.id),user_id)
    if session.query(RuntimeOperation).filter(RuntimeOperation.runtime_id == runtime.id,
            ((RuntimeOperation.state.in_(['preparing','switched'])) | (
                (RuntimeOperation.state == 'cancelled') & (RuntimeOperation.candidate_container_id == runtime.container_id)
            ))).first() is not None:
        return start(session,workspace,user_id)
    try:
        container = _client().containers.get(runtime.container_id)
        if not _container_uses_configured_image(container):
            return start(session, workspace, user_id)
        container.restart(timeout=3)
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
        logger.error("重启 OpenCode Runtime 失败：workspace_id={}, user_id={}, error_type={}", workspace.id, user_id, type(exc).__name__)
    return workspace


def remove_container(workspace: Workspace, session: Session | None = None) -> None:
    """Detach resources atomically; the Worker removes committed identities."""
    if session is not None:
        from app.infra.runtime_container_deletion import stage_container_deletion_fallback
        for runtime in runtime_service.list_runtimes(session, int(workspace.id)):
            remove_runtime(runtime, session)
        stage_container_deletion_fallback(session, workspace.container_id)
        workspace.container_id = None
        workspace.status = "stopped"
        workspace.error_message = None
        session.flush()
        return
    # Explicit nontransactional callers retain the synchronous RPC contract.
    if workspace.container_id:
        _remove_container_now(workspace.container_id)


def _remove_container_now(container_id: str) -> None:
    try:
        _client().containers.get(container_id).remove(force=True, v=True)
    except ContainerNotFound:
        pass


def remove_runtime(runtime: OpenCodeRuntime, session: Session | None = None) -> None:
    """Revoke access and stage removal in the caller's transaction."""
    if session is not None:
        from app.infra.runtime_container_deletion import stage_container_deletion_fallback
        operations.cancel_runtime(session,int(runtime.id))
        stage_container_deletion_fallback(session, runtime.container_id)
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        runtime.container_id = None
        runtime.status = "stopped"
        runtime.error_message = None
        runtime.last_used_at = utc_now()
        session.flush()
    elif runtime.container_id:
        _remove_container_now(runtime.container_id)


def get_access_url(entity: Workspace | OpenCodeRuntime) -> str | None:
    if not entity.container_id:
        return None
    try:
        ports = _client().containers.get(entity.container_id).attrs["NetworkSettings"]["Ports"]
        mapping = ports.get("3000/tcp") or []
        if mapping and mapping[0].get("HostPort"):
            return f"http://localhost:{mapping[0]['HostPort']}"
    except DockerException as exc:
        logger.warning("读取 OpenCode 访问地址失败：entity_id={}, error_type={}", getattr(entity, "id", None), type(exc).__name__)
    return None


def merge_opencode_mcp_config(existing: dict | None, mcp_config: dict) -> dict:
    return runtime_config.merge_opencode_mcp_config(existing, mcp_config)


def _chat_api_runtime_config(runtime_token: str) -> dict[str, str]:
    """Expose only the gateway and scoped credential to untrusted code."""
    configured = get_chat_model_config()
    base_url = os.getenv("OPENCODE_LLM_GATEWAY_URL")
    if not base_url:
        base_url = "http://skycloud-llm-gateway:5002/v1"
    return {
        "base_url": base_url.rstrip("/"),
        "model": configured["model"].strip(),
        "api_key": runtime_token,
    }


def _model_config_policy() -> dict:
    return {
        'provider_id': OPENCODE_CHAT_PROVIDER_ID,
        'output_limit': runtime_llm_tokens.positive_setting('RUNTIME_LLM_MAX_OUTPUT_TOKENS', 4096),
        'context_limit': runtime_llm_tokens.positive_setting('RUNTIME_LLM_CONTEXT_TOKENS', 65536),
    }


def merge_opencode_chat_provider(existing: dict | None, chat_config: dict[str, str] | None = None) -> dict:
    return runtime_config.merge_opencode_chat_provider(existing, chat_config, **_model_config_policy())


def _opencode_chat_provider_needs_reload(existing: dict, chat_config: dict[str, str]) -> bool:
    return runtime_config._opencode_chat_provider_needs_reload(existing, chat_config, **_model_config_policy())


def merge_opencode_expert_agent(existing: dict | None) -> dict:
    return runtime_config.merge_opencode_expert_agent(existing, agent_name=OPENCODE_EXPERT_AGENT)


def _read_opencode_config(container) -> dict:
    return container.read_config()


def _write_opencode_config(container, config: dict) -> None:
    container.write_config(config)


@serialized
def setup_mcp(
        session: Session,
        workspace: Workspace,
        user_id: int,
        *,
        runtime: OpenCodeRuntime | None = None,
        reload_runtime_config: bool = False,
        generation: str | None = None,
) -> None:
    """Issue a scoped runtime token and atomically merge its MCP config.

    Configuration is reloaded through the instance API, preserving bounded
    tmpfs storage and keeping all provisioning commands under the runtime UID.
    """

    if runtime is None:
        runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if runtime is None:
        raise BusinessRuleError("Runtime has not been created")

    if runtime.status != "running" or not runtime.container_id:
        raise BusinessRuleError("SKYcode 工作区未运行")

    runtime_id, container_id = int(runtime.id), runtime.container_id
    workspace_id = int(workspace.id)
    runtime = operations.current_runtime(session,workspace_id,user_id,runtime_id,container_id,generation)
    role = get_member_role(session, workspace_id, user_id)
    if role is None:
        raise BusinessRuleError("用户不是该工作空间成员")

    record, token = runtime_token_service.refresh_runtime_token(
        session,
        runtime,
        role=role.value,
    )
    config = {
        "type": "remote",
        "url": _mcp_endpoint(),
        "enabled": True,
        "oauth": False,
        "headers": {"Authorization": f"Bearer {token}"},
    }
    try:
        container = _client().containers.get(runtime.container_id)
        existing = _read_opencode_config(container)
        runtime = operations.current_runtime(session,workspace_id,user_id,runtime_id,container_id,generation)
        llm_token = runtime_llm_tokens.issue_token(session, runtime)
        runtime = operations.current_runtime(session,workspace_id,user_id,runtime_id,container_id,generation)
        chat_config = _chat_api_runtime_config(llm_token)
        provider_reload_needed = _opencode_chat_provider_needs_reload(
            existing,
            chat_config,
        )
        merged = merge_opencode_mcp_config(existing, config)
        merged = merge_opencode_chat_provider(merged, chat_config)
        merged = merge_opencode_expert_agent(merged)
        _sync_mcp_via_runtime_api(
            workspace, user_id, runtime, config,
            reload_config=reload_runtime_config or provider_reload_needed,
            runtime_config=merged,
        )
        _write_opencode_config(container, merged)
        runtime.config_version = int(runtime.config_version or 0) + 1
        runtime.last_used_at = utc_now()
        session.commit()
        logger.info(
            "已配置 OpenCode MCP：workspace_id={}, user_id={}, runtime_id={}, token_id={}, config_version={}",
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
            "配置 OpenCode MCP 失败：workspace_id={}, user_id={}, runtime_id={}, error_type={}",
            workspace.id,
            user_id,
            runtime.id,
            type(exc).__name__,
        )
        raise


def _create(workspace: Workspace, user_id: int, runtime_id: int | None = None):
    if not runtime_id:
        raise BusinessRuleError("Runtime identity is required")
    # Policy and Docker access exist only in the restricted controller.
    container = _client().create(int(workspace.id), int(user_id), int(runtime_id))
    logger.info("Runtime Controller 已创建容器：runtime_id={}, container={}", runtime_id, container.short_id)
    return container
