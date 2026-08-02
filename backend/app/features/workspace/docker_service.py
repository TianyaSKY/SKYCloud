"""OpenCode Docker lifecycle and per-runtime MCP configuration."""

import base64
import copy
import json
import os
import shlex
import socket
import time
import uuid

import docker
import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError
from app.features.workspace import runtime_service
from app.features.workspace.permissions import get_member_role
from app.features.assistant.clients.opencode_auth import server_password, server_username
from app.infra.datetime_utils import beijing_now
from app.mcp import runtime_token_service
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.workspace import Workspace


def _docker_exception_type(name: str) -> type[Exception]:
    """测试环境可能以轻量 mock 替代 docker 模块，避免导入子模块失败。"""
    candidate = getattr(getattr(docker, "errors", None), name, None)
    return candidate if isinstance(candidate, type) and issubclass(candidate, Exception) else Exception


DockerException = _docker_exception_type("DockerException")
ContainerNotFound = _docker_exception_type("NotFound")
ImageNotFound = _docker_exception_type("ImageNotFound")

# Use the upstream image directly. The lifecycle code pulls it on demand, so
# local development does not require a separate project-owned image build.
OPENCODE_IMAGE = os.getenv("OPENCODE_IMAGE", "ghcr.io/anomalyco/opencode:latest")
SKYCLOUD_DOCKER_NETWORK = os.getenv("SKYCLOUD_DOCKER_NETWORK", "skycloud_skycloud-network")
WORKSPACE_MEM_LIMIT = os.getenv("WORKSPACE_MEM_LIMIT", "1g")
WORKSPACE_CPU_QUOTA = int(os.getenv("WORKSPACE_CPU_QUOTA", "100000"))
WORKSPACE_CPU_PERIOD = int(os.getenv("WORKSPACE_CPU_PERIOD", "100000"))
MCP_PORT = int(os.getenv("MCP_PORT", "5001"))
OPENCODE_CONFIG_PATH = "/root/.config/opencode/opencode.json"
OPENCODE_EXPERT_AGENT = os.getenv("OPENCODE_EXPERT_AGENT", "skycloud-expert")


def _client() -> docker.DockerClient:
    docker_host = os.getenv("DOCKER_HOST")
    return docker.DockerClient(base_url=docker_host) if docker_host else docker.from_env()


def _ensure_opencode_image(client: docker.DockerClient) -> None:
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
            "无法检查 OpenCode Runtime 镜像：runtime_id={}, reason={}",
            runtime.id,
            exc,
        )
        # Do not destroy a healthy Runtime merely because Docker is
        # temporarily unavailable; the caller will surface its normal error.
        return True
    return _container_uses_configured_image(container)


def _api_runs_in_docker() -> bool:
    """Whether this API process can address runtime containers by Docker DNS."""

    configured = os.getenv("SKYCLOUD_RUNTIME_NETWORK_MODE", "").strip().lower()
    if configured in {"docker", "container"}:
        return True
    if configured in {"host", "local"}:
        return False
    return os.path.exists("/.dockerenv")


def get_runtime_base_url(
    workspace: Workspace,
    user_id: int,
    runtime: OpenCodeRuntime | None = None,
) -> str:
    """Return a Runtime URL that works for Docker and hybrid local setups.

    A Dockerized API reaches a Runtime by its Docker-network hostname.  When
    the API is launched directly on the host (the common macOS development
    setup), it must use the Runtime's loopback-only published port instead.
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

    if not _api_runs_in_docker() and runtime is not None:
        access_url = get_access_url(runtime)
        if access_url:
            return access_url
    return f"http://{_name(workspace, user_id)}:3000"


def _mcp_endpoint() -> str:
    configured = os.getenv("OPENCODE_MCP_URL")
    if configured:
        return configured.rstrip("/")
    if _api_runs_in_docker():
        return f"http://skycloud-backend-mcp:{MCP_PORT}/mcp"
    # Docker Desktop for macOS/Windows resolves this host name to the host
    # system, so a Runtime can reach an MCP server started directly by Python.
    return f"http://host.docker.internal:{MCP_PORT}/mcp"


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
) -> None:
    """Apply fresh MCP credentials to the live Runtime without a restart."""

    base_url = get_runtime_base_url(workspace, user_id, runtime)
    auth = httpx.BasicAuth(
        server_username(),
        server_password(int(runtime.id), int(user_id), int(workspace.id)),
    )
    deadline = time.monotonic() + _startup_timeout_seconds()
    last_error: str | None = None
    with httpx.Client(base_url=base_url, auth=auth, timeout=3.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get("/global/health")
                if response.status_code == 200:
                    break
                if response.status_code == 401:
                    raise DockerException("OpenCode Runtime 认证失败")
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = exc.__class__.__name__
            time.sleep(0.25)
        else:
            detail = f"（最后错误：{last_error}）" if last_error else ""
            raise DockerException(f"OpenCode Runtime 未在启动超时内就绪{detail}")

        try:
            response = client.post("/mcp", json={"name": "SKYCLOUD", "config": config})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DockerException("向 OpenCode Runtime 注入 MCP 配置失败") from exc


def _random_loopback_port() -> int:
    """Reserve a host-port candidate for a loopback-only Docker binding."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
        logger.warning("同步 OpenCode 容器状态失败：workspace_id={}, reason={}", workspace.id, exc)


def _sync_runtime_status(session: Session, runtime: OpenCodeRuntime) -> None:
    if not runtime.container_id:
        return
    try:
        container = _client().containers.get(runtime.container_id)
        status = "running" if container.status == "running" else "stopped"
        if runtime.status != status:
            runtime.status = status
            runtime.last_used_at = beijing_now()
            session.commit()
    except ContainerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
        runtime.last_used_at = beijing_now()
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
    except DockerException as exc:
        logger.warning("同步 OpenCode Runtime 状态失败：runtime_id={}, reason={}", runtime.id, exc)


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
    return message[:500]


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


def start(session: Session, workspace: Workspace, user_id: int) -> Workspace:
    """Start the caller's per-workspace runtime and provision a fresh token."""

    runtime = runtime_service.get_or_create_runtime(session, workspace.id, user_id)
    created = False
    try:
        if runtime.container_id:
            try:
                container = _client().containers.get(runtime.container_id)
                if _container_uses_configured_image(container):
                    container.start()
                else:
                    logger.info(
                        "OpenCode Runtime 镜像已更新，正在重建：workspace_id={}, user_id={}, image={}",
                        workspace.id,
                        user_id,
                        OPENCODE_IMAGE,
                    )
                    container.remove(force=True)
                    container = _create(workspace, user_id, runtime.id)
                    created = True
            except ContainerNotFound:
                container = _create(workspace, user_id, runtime.id)
                created = True
        else:
            container = _create(workspace, user_id, runtime.id)
            created = True

        runtime.container_id = container.id
        runtime.status = "running"
        runtime.error_message = None
        runtime.last_started_at = beijing_now()
        runtime.last_used_at = beijing_now()
        session.commit()
        setup_mcp(
            session,
            workspace,
            user_id,
            runtime=runtime,
            reload_runtime_config=created,
        )
    except DockerException as exc:
        session.rollback()
        runtime = runtime_service.get_or_create_runtime(session, workspace.id, user_id)
        runtime.status = "error"
        runtime.error_message = _runtime_error_message(exc)
        session.commit()
        logger.exception("启动 OpenCode Runtime 失败：workspace_id={}, user_id={}, reason={}", workspace.id, user_id, exc)
    return workspace


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
            workspace.error_message = str(exc)[:500]
            logger.exception("停止 OpenCode 容器失败：workspace_id={}, reason={}", workspace.id, exc)
        session.commit()
        return workspace

    runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if not runtime:
        return workspace
    try:
        if runtime.container_id:
            _client().containers.get(runtime.container_id).stop(timeout=10)
        runtime.status = "stopped"
        runtime.error_message = None
        runtime.last_used_at = beijing_now()
    except ContainerNotFound:
        runtime.container_id = None
        runtime.status = "stopped"
    except DockerException as exc:
        runtime.status = "error"
        runtime.error_message = str(exc)[:500]
        logger.exception("停止 OpenCode Runtime 失败：workspace_id={}, user_id={}, reason={}", workspace.id, user_id, exc)
    runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
    session.commit()
    return workspace


def restart(session: Session, workspace: Workspace, user_id: int) -> Workspace:
    runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if not runtime or not runtime.container_id:
        return start(session, workspace, user_id)
    try:
        _client().containers.get(runtime.container_id).restart(timeout=3)
        runtime.status = "running"
        runtime.error_message = None
        runtime.last_started_at = beijing_now()
        runtime.last_used_at = beijing_now()
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
        runtime.error_message = str(exc)[:500]
        session.commit()
        logger.exception("重启 OpenCode Runtime 失败：workspace_id={}, user_id={}, reason={}", workspace.id, user_id, exc)
    return workspace


def remove_container(workspace: Workspace, session: Session | None = None) -> None:
    """Best-effort cleanup for all runtimes before workspace deletion."""

    if session is not None:
        runtimes = runtime_service.list_runtimes(session, int(workspace.id))
        for runtime in runtimes:
            if runtime.container_id:
                try:
                    _client().containers.get(runtime.container_id).remove(force=True, v=True)
                except (ContainerNotFound, DockerException) as exc:
                    logger.warning("清理 OpenCode Runtime 失败：runtime_id={}, reason={}", runtime.id, exc)
            runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
            runtime.container_id = None
            runtime.status = "stopped"
        if runtimes:
            session.flush()

    if workspace.container_id:
        try:
            _client().containers.get(workspace.container_id).remove(force=True, v=True)
        except (ContainerNotFound, DockerException) as exc:
            logger.warning("清理旧 OpenCode 容器失败：workspace_id={}, reason={}", workspace.id, exc)


def remove_runtime(runtime: OpenCodeRuntime, session: Session | None = None) -> None:
    """Remove exactly one member runtime and revoke its credentials."""

    if runtime.container_id:
        try:
            _client().containers.get(runtime.container_id).remove(force=True, v=True)
        except (ContainerNotFound, DockerException) as exc:
            logger.warning("清理 OpenCode Runtime 失败：runtime_id={}, reason={}", runtime.id, exc)
    if session is not None:
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        runtime.container_id = None
        runtime.status = "stopped"
        runtime.error_message = None
        runtime.last_used_at = beijing_now()
        session.flush()


def get_access_url(entity: Workspace | OpenCodeRuntime) -> str | None:
    if not entity.container_id:
        return None
    try:
        ports = _client().containers.get(entity.container_id).attrs["NetworkSettings"]["Ports"]
        mapping = ports.get("3000/tcp") or []
        if mapping and mapping[0].get("HostPort"):
            return f"http://localhost:{mapping[0]['HostPort']}"
    except DockerException as exc:
        logger.warning("读取 OpenCode 访问地址失败：entity_id={}, reason={}", getattr(entity, "id", None), exc)
    return None


def merge_opencode_mcp_config(existing: dict | None, mcp_config: dict) -> dict:
    """Merge only ``mcp.SKYCLOUD`` while preserving all other config."""

    result = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    mcp = result.get("mcp")
    mcp = copy.deepcopy(mcp) if isinstance(mcp, dict) else {}
    mcp["SKYCLOUD"] = copy.deepcopy(mcp_config)
    result["mcp"] = mcp
    return result


def merge_opencode_expert_agent(existing: dict | None) -> dict:
    """Install the least-privilege SKYCloud expert agent without replacing
    unrelated user configuration.

    The provider still asks for high-risk operations; the MCP server remains
    the final workspace and role boundary.
    """

    result = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    agents = result.get("agent")
    agents = copy.deepcopy(agents) if isinstance(agents, dict) else {}
    agents[OPENCODE_EXPERT_AGENT] = {
        "description": "SKYCloud workspace expert",
        "prompt": (
            "You are the SKYCloud workspace expert. Operate only in the current workspace. "
            "Use SKYCloud MCP for cloud-drive writes. Never expose credentials or upload data "
            "externally. Ask before deletion, overwrite, publication, or risky shell commands."
        ),
        "permission": {
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "edit": "ask",
            "bash": {
                "*": "ask",
                "rg *": "allow",
                "grep *": "allow",
                "find *": "allow",
                "git status *": "allow",
                "git diff *": "allow",
                "git push *": "deny",
            },
        },
    }
    result["agent"] = agents
    return result


def _exec_exit_code(result) -> int:
    code = getattr(result, "exit_code", 0)
    return code if isinstance(code, int) else 0


def _exec_output(result) -> bytes:
    output = getattr(result, "output", b"")
    if output is None:
        return b""
    if isinstance(output, bytes):
        return output
    if isinstance(output, str):
        return output.encode()
    return bytes(output) if isinstance(output, bytearray) else b""


def _read_opencode_config(container) -> dict:
    try:
        result = container.exec_run(["cat", OPENCODE_CONFIG_PATH], user="root")
        if _exec_exit_code(result) != 0:
            return {}
        raw = _exec_output(result).decode("utf-8", errors="replace").strip()
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except (DockerException, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _write_opencode_config(container, config: dict) -> None:
    encoded = base64.b64encode(
        json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    temp_path = f"{OPENCODE_CONFIG_PATH}.tmp.{uuid.uuid4().hex}"
    command = (
        f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(temp_path)} "
        f"&& mv {shlex.quote(temp_path)} {shlex.quote(OPENCODE_CONFIG_PATH)}"
    )
    result = container.exec_run(["sh", "-c", command], user="root")
    if _exec_exit_code(result) != 0:
        raise DockerException("写入 OpenCode MCP 配置失败")


def setup_mcp(
        session: Session,
        workspace: Workspace,
        user_id: int,
        *,
        runtime: OpenCodeRuntime | None = None,
        reload_runtime_config: bool = False,
) -> None:
    """Issue a scoped runtime token and atomically merge its MCP config.

    The official image reads the agent config only when the server starts.
    Newly created Runtimes therefore restart once after the initial write;
    later token refreshes are applied through the live MCP API without a
    disruptive restart.
    """

    if runtime is None:
        runtime = runtime_service.get_runtime(session, workspace.id, user_id)
    if runtime is None:
        runtime = runtime_service.get_or_create_runtime(session, workspace.id, user_id)

    if runtime.status != "running" or not runtime.container_id:
        raise BusinessRuleError("SKYcode 工作区未运行")

    role = get_member_role(session, workspace.id, user_id)
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
        mkdir_result = container.exec_run(
            ["mkdir", "-p", "/root/.config/opencode"], user="root"
        )
        if _exec_exit_code(mkdir_result) != 0:
            raise DockerException("创建 OpenCode 配置目录失败")
        existing = _read_opencode_config(container)
        merged = merge_opencode_mcp_config(existing, config)
        merged = merge_opencode_expert_agent(merged)
        _write_opencode_config(container, merged)
        if reload_runtime_config:
            container.restart(timeout=3)
        _sync_mcp_via_runtime_api(workspace, user_id, runtime, config)
        runtime.config_version = int(runtime.config_version or 0) + 1
        runtime.last_used_at = beijing_now()
        session.commit()
        logger.info(
            "已配置 OpenCode MCP：workspace_id={}, user_id={}, runtime_id={}, token_id={}, config_version={}",
            workspace.id,
            user_id,
            runtime.id,
            record.id,
            runtime.config_version,
        )
    except DockerException:
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
        logger.exception(
            "配置 OpenCode MCP 失败：workspace_id={}, user_id={}, runtime_id={}",
            workspace.id,
            user_id,
            runtime.id,
        )
        raise


def _create(
        workspace: Workspace,
        user_id: int,
        runtime_id: int | None = None,
):
    client = _client()
    _ensure_opencode_image(client)
    try:
        client.containers.get(_name(workspace, user_id)).remove(force=True)
    except (ContainerNotFound, DockerException):
        pass
    container = client.containers.run(
        image=OPENCODE_IMAGE,
        name=_name(workspace, user_id),
        detach=True,
        # The upstream image's ENTRYPOINT is ``opencode``.
        command=["serve", "--hostname", "0.0.0.0", "--port", "3000"],
        environment={
            "SKYCLOUD_WORKSPACE_ID": str(workspace.id),
            "SKYCLOUD_USER_ID": str(user_id),
            "SKYCLOUD_RUNTIME_ID": str(runtime_id or ""),
            "OPENCODE_SERVER_USERNAME": server_username(),
            "OPENCODE_SERVER_PASSWORD": server_password(
                int(runtime_id or 0), int(user_id), int(workspace.id)
            ),
        },
        mem_limit=WORKSPACE_MEM_LIMIT,
        cpu_quota=WORKSPACE_CPU_QUOTA,
        cpu_period=WORKSPACE_CPU_PERIOD,
        network=SKYCLOUD_DOCKER_NETWORK,
        restart_policy={"Name": "unless-stopped"},
        # Keep the random host port reachable only from the local machine.
        # SKYCloud's backend uses the Docker network hostname for assistant
        # traffic; exposing OpenCode on 0.0.0.0 would bypass SKYCloud auth.
        ports={"3000/tcp": ("127.0.0.1", _random_loopback_port())},
        labels={
            "skycloud.component": "opencode-runtime",
            "skycloud.workspace_id": str(workspace.id),
            "skycloud.user_id": str(user_id),
            "skycloud.runtime_id": str(runtime_id or ""),
        },
    )
    logger.info(
        "OpenCode Runtime 已启动：workspace_id={}, user_id={}, runtime_id={}, container={}",
        workspace.id,
        user_id,
        runtime_id,
        container.short_id,
    )
    return container
