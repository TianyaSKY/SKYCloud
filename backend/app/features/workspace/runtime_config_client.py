"""Bounded synchronous OpenCode configuration RPC, independent of SQL lifecycle.

Call from an existing worker/thread boundary; the caller owns fresh identity
checks and transaction decisions before and after this external operation.
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
    """Time and reload policy for one configuration push."""

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
    """Wait for Runtime readiness, then push MCP and optional provider config."""
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
                # The pinned OpenCode caches global config for its lifetime.
                # Instance disposal alone keeps the previous model JWT. Let
                # updateGlobal write the changed config and invalidate that
                # cache before the Controller's final normalized file write.
                response = client.patch("/global/config", json=options.runtime_config)
                response.raise_for_status()
                # Reload the instance without restarting the container: tmpfs
                # configuration must remain mounted, and no active run exists
                # during provisioning.
                response = client.post("/instance/dispose")
                response.raise_for_status()
            response = client.post("/mcp", json={"name": "SKYCLOUD", "config": config})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            message = MCP_INJECTION_FAILED
            raise ControllerError(message) from exc
