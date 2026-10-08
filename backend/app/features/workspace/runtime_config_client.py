"""Bounded synchronous OpenCode configuration RPC, independent of SQL lifecycle.

Call from an existing worker/thread boundary; the caller owns fresh identity
checks and transaction decisions before and after this external operation.
"""
import time
import httpx
from runtime_controller.client import ControllerError


def sync_mcp(base_url: str, auth: httpx.BasicAuth, headers: dict, config: dict, *,
             startup_timeout: float, reload_config: bool = False, runtime_config: dict | None = None) -> None:
    deadline = time.monotonic() + startup_timeout
    last_error: str | None = None
    with httpx.Client(base_url=base_url, auth=auth, headers=headers, timeout=3.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get("/global/health")
                if response.status_code == 200:
                    break
                if response.status_code == 401:
                    raise ControllerError("OpenCode Runtime 认证失败")
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = exc.__class__.__name__
            time.sleep(0.25)
        else:
            detail = f"（最后错误：{last_error}）" if last_error else ""
            raise ControllerError(f"OpenCode Runtime 未在启动超时内就绪{detail}")

        try:
            if reload_config:
                # The pinned OpenCode caches global config for its lifetime.
                # Instance disposal alone keeps the previous model JWT. Let
                # updateGlobal write the changed config and invalidate that
                # cache before the Controller's final normalized file write.
                response = client.patch('/global/config',json=runtime_config)
                response.raise_for_status()
                # Reload the instance without restarting the container: tmpfs
                # configuration must remain mounted, and no active run exists
                # during provisioning.
                response = client.post("/instance/dispose")
                response.raise_for_status()
            response = client.post("/mcp", json={"name": "SKYCLOUD", "config": config})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ControllerError("向 OpenCode Runtime 注入 MCP 配置失败") from exc




