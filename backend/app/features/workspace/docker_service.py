"""OpenCode Docker 工作区生命周期。

容器从属于协作工作空间；所有调用方须在进入本模块前完成成员权限校验。
"""

import json
import os

import docker
from loguru import logger
from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError
from app.mcp import token_service as mcp_token_service
from app.models.workspace import Workspace


def _docker_exception_type(name: str) -> type[Exception]:
    """测试环境可能以轻量 mock 替代 docker 模块，避免导入子模块失败。"""
    candidate = getattr(getattr(docker, "errors", None), name, None)
    return candidate if isinstance(candidate, type) and issubclass(candidate, Exception) else Exception


DockerException = _docker_exception_type("DockerException")
ContainerNotFound = _docker_exception_type("NotFound")

OPENCODE_IMAGE = os.getenv("OPENCODE_IMAGE", "skycloud/opencode-workspace:latest")
SKYCLOUD_DOCKER_NETWORK = os.getenv("SKYCLOUD_DOCKER_NETWORK", "skycloud_skycloud-network")
WORKSPACE_MEM_LIMIT = os.getenv("WORKSPACE_MEM_LIMIT", "1g")
WORKSPACE_CPU_QUOTA = int(os.getenv("WORKSPACE_CPU_QUOTA", "100000"))
WORKSPACE_CPU_PERIOD = int(os.getenv("WORKSPACE_CPU_PERIOD", "100000"))
MCP_PORT = int(os.getenv("MCP_PORT", "5001"))
OPENCODE_CONFIG_PATH = "/root/.config/opencode/opencode.json"


def _client() -> docker.DockerClient:
    docker_host = os.getenv("DOCKER_HOST")
    return docker.DockerClient(base_url=docker_host) if docker_host else docker.from_env()


def _name(workspace: Workspace) -> str:
    return f"skycloud-workspace-{workspace.id}"


def _sync_status(session: Session, workspace: Workspace) -> None:
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


def summary(session: Session, workspace: Workspace) -> dict:
    """返回容器摘要；列表场景调用此函数会尽力同步实际状态。"""
    _sync_status(session, workspace)
    return {
        "container_id": workspace.container_id[:12] if workspace.container_id else None,
        "status": workspace.status,
        "error_message": workspace.error_message,
        "access_url": get_access_url(workspace) if workspace.status == "running" else None,
    }


def start(session: Session, workspace: Workspace, user_id: int) -> Workspace:
    """启动或重建一个工作空间的 OpenCode 容器，并写入调用用户的 MCP 配置。"""
    try:
        if workspace.container_id:
            try:
                container = _client().containers.get(workspace.container_id)
                container.start()
            except ContainerNotFound:
                container = _create(workspace)
        else:
            container = _create(workspace)
        workspace.container_id = container.id
        workspace.status = "running"
        workspace.error_message = None
        session.commit()
        setup_mcp(session, workspace, user_id)
    except DockerException as exc:
        session.rollback()
        workspace.status = "error"
        workspace.error_message = str(exc)[:500]
        session.commit()
        logger.exception("启动 OpenCode 容器失败：workspace_id={}, reason={}", workspace.id, exc)
    return workspace


def stop(session: Session, workspace: Workspace) -> Workspace:
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


def restart(session: Session, workspace: Workspace, user_id: int) -> Workspace:
    if not workspace.container_id:
        return start(session, workspace, user_id)
    try:
        _client().containers.get(workspace.container_id).restart(timeout=3)
        workspace.status = "running"
        workspace.error_message = None
        session.commit()
        setup_mcp(session, workspace, user_id)
    except ContainerNotFound:
        workspace.container_id = None
        session.commit()
        return start(session, workspace, user_id)
    except DockerException as exc:
        workspace.status = "error"
        workspace.error_message = str(exc)[:500]
        session.commit()
        logger.exception("重启 OpenCode 容器失败：workspace_id={}, reason={}", workspace.id, exc)
    return workspace


def remove_container(workspace: Workspace) -> None:
    """删除空间前的尽力清理；失败不阻止协作空间删除。"""
    if not workspace.container_id:
        return
    try:
        _client().containers.get(workspace.container_id).remove(force=True, v=True)
    except (ContainerNotFound, DockerException) as exc:
        logger.warning("清理 OpenCode 容器失败：workspace_id={}, reason={}", workspace.id, exc)


def get_access_url(workspace: Workspace) -> str | None:
    if not workspace.container_id:
        return None
    try:
        ports = _client().containers.get(workspace.container_id).attrs["NetworkSettings"]["Ports"]
        mapping = ports.get("3000/tcp") or []
        if mapping and mapping[0].get("HostPort"):
            return f"http://localhost:{mapping[0]['HostPort']}"
    except DockerException as exc:
        logger.warning("读取 OpenCode 访问地址失败：workspace_id={}, reason={}", workspace.id, exc)
    return None


def setup_mcp(session: Session, workspace: Workspace, user_id: int) -> None:
    if workspace.status != "running" or not workspace.container_id:
        raise BusinessRuleError("OpenCode 工作区未运行")
    record, token = mcp_token_service.ensure_user_mcp_token(session, user_id)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"SKYCLOUD": {"type": "remote", "url": f"http://skycloud-backend-mcp:{MCP_PORT}/mcp", "enabled": True, "oauth": False, "headers": {"Authorization": f"Bearer {token}"}}},
    }
    try:
        container = _client().containers.get(workspace.container_id)
        container.exec_run(["mkdir", "-p", "/root/.config/opencode"], user="root")
        escaped = json.dumps(config, ensure_ascii=False).replace("'", "'\\''")
        result = container.exec_run(["sh", "-c", f"echo '{escaped}' > {OPENCODE_CONFIG_PATH}"], user="root")
        if result.exit_code != 0:
            raise DockerException("写入 OpenCode MCP 配置失败")
        logger.info("已配置 OpenCode MCP：workspace_id={}, user_id={}, token_id={}", workspace.id, user_id, record.id)
    except DockerException:
        logger.exception("配置 OpenCode MCP 失败：workspace_id={}, user_id={}", workspace.id, user_id)
        raise


def _create(workspace: Workspace):
    client = _client()
    try:
        client.containers.get(_name(workspace)).remove(force=True)
    except (ContainerNotFound, DockerException):
        pass
    container = client.containers.run(
        image=OPENCODE_IMAGE,
        name=_name(workspace),
        detach=True,
        environment={"SKYCLOUD_WORKSPACE_ID": str(workspace.id)},
        mem_limit=WORKSPACE_MEM_LIMIT,
        cpu_quota=WORKSPACE_CPU_QUOTA,
        cpu_period=WORKSPACE_CPU_PERIOD,
        network=SKYCLOUD_DOCKER_NETWORK,
        restart_policy={"Name": "unless-stopped"},
        ports={"3000/tcp": None},
        labels={"skycloud.component": "workspace", "skycloud.workspace_id": str(workspace.id)},
    )
    logger.info("OpenCode 容器已启动：workspace_id={}, container={}", workspace.id, container.short_id)
    return container
