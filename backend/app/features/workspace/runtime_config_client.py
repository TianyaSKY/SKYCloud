"""有界的同步 OpenCode 配置 RPC, 独立于 SQL 生命周期.

应在现有 Worker 或独立线程边界内调用; 调用方在执行此前后的
身份最新状态检查与事务决策由自身全权负责。
"""

import time
from dataclasses import dataclass

import httpx
from runtime_common.json_types import JsonObject
from runtime_controller.client import ControllerError

READY_POLL_SECONDS = 0.25
HEALTHY_STATUS = 200
UNAUTHORIZED_STATUS = 401
HTTP_TIMEOUT_SECONDS = 3.0

STARTUP_NOT_READY = "OpenCode Runtime 未在启动超时内就绪"
AUTH_FAILED = "OpenCode Runtime 认证失败"
MCP_INJECTION_FAILED = "向 OpenCode Runtime 注入 MCP 配置失败"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigSyncOptions:
    """单次配置推送的超时与重载策略配置."""

    startup_timeout: float
    reload_config: bool = False
    runtime_config: "JsonObject | None" = None


def sync_mcp(
    base_url: str,
    auth: httpx.BasicAuth,
    headers: dict[str, str],
    config: JsonObject,
    options: ConfigSyncOptions,
) -> None:
    """等待 Runtime 就绪, 随后推送 MCP 与可选的供应商配置."""
    deadline = time.monotonic() + options.startup_timeout
    last_error: str | None = None
    with httpx.Client(
        base_url=base_url,
        auth=auth,
        headers=headers,
        timeout=HTTP_TIMEOUT_SECONDS,
        trust_env=False,
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get("/global/health")
                if response.status_code == HEALTHY_STATUS:
                    break
                if response.status_code == UNAUTHORIZED_STATUS:
                    message = AUTH_FAILED
                    raise ControllerError(message)
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = exc.__class__.__name__
            time.sleep(READY_POLL_SECONDS)
        else:
            detail = f"(最后错误: {last_error})" if last_error else ""
            message = STARTUP_NOT_READY + detail
            raise ControllerError(message)

        try:
            if options.reload_config:
                # 固定版本的 OpenCode 会在其生命周期内缓存全局配置。
                # 仅销毁实例仍会保留先前的模型 JWT。通过调用
                # updateGlobal 写入变更配置并在 Controller 最终规范化
                # 写入文件之前作废该内存缓存。
                response = client.patch("/global/config", json=options.runtime_config)
                response.raise_for_status()
                # 重新加载实例而无需重启容器: tmpfs 配置目录
                # 必须保持挂载, 且在配置编排期间
                # 不存在活跃运行。
                response = client.post("/instance/dispose")
                response.raise_for_status()
            response = client.post("/mcp", json={"name": "SKYCLOUD", "config": config})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            message = MCP_INJECTION_FAILED
            raise ControllerError(message) from exc
