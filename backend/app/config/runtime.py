"""OpenCode 沙箱运行时与模型网关配额强类型配置."""

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_RUNTIME_LLM_TOKEN_TTL_SECONDS: Final[int] = 3600
DEFAULT_RUNTIME_LLM_HOURLY_TOKEN_BUDGET: Final[int] = 100000
DEFAULT_RUNTIME_LLM_HOURLY_REQUEST_LIMIT: Final[int] = 60
DEFAULT_RUNTIME_LLM_MAX_OUTPUT_TOKENS: Final[int] = 4096
DEFAULT_RUNTIME_LLM_CONTEXT_TOKENS: Final[int] = 65536

DEFAULT_WORKSPACE_MEM_LIMIT: Final[str] = "1g"
DEFAULT_WORKSPACE_CPU_QUOTA: Final[int] = 100000
DEFAULT_WORKSPACE_CPU_PERIOD: Final[int] = 100000
DEFAULT_MAX_WORKSPACES_PER_USER: Final[int] = 3
DEFAULT_OPENCODE_STARTUP_TIMEOUT: Final[float] = 30.0


def _read_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except (TypeError, ValueError):
        return default
    else:
        return val if val > 0 else default


def _read_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
    except (TypeError, ValueError):
        return default
    else:
        return val if val > 0 else default


@dataclass(frozen=True, slots=True)
class RuntimeLlmConfig:
    """沙箱内部 LLM 代理配额与上下文限制."""

    token_ttl_seconds: int = DEFAULT_RUNTIME_LLM_TOKEN_TTL_SECONDS
    hourly_token_budget: int = DEFAULT_RUNTIME_LLM_HOURLY_TOKEN_BUDGET
    hourly_request_limit: int = DEFAULT_RUNTIME_LLM_HOURLY_REQUEST_LIMIT
    max_output_tokens: int = DEFAULT_RUNTIME_LLM_MAX_OUTPUT_TOKENS
    context_tokens: int = DEFAULT_RUNTIME_LLM_CONTEXT_TOKENS

    def __post_init__(self) -> None:
        """校验运行时 LLM 配额边界约束."""
        if self.token_ttl_seconds <= 0:
            msg = "token_ttl_seconds must be > 0"
            raise ValueError(msg)
        if self.hourly_token_budget <= 0:
            msg = "hourly_token_budget must be > 0"
            raise ValueError(msg)
        if self.hourly_request_limit <= 0:
            msg = "hourly_request_limit must be > 0"
            raise ValueError(msg)
        if self.max_output_tokens <= 0:
            msg = "max_output_tokens must be > 0"
            raise ValueError(msg)
        if self.context_tokens <= self.max_output_tokens:
            msg = "context_tokens must exceed max_output_tokens"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WorkspaceResourceConfig:
    """用户工作区沙箱容器资源与数量限制."""

    mem_limit: str = DEFAULT_WORKSPACE_MEM_LIMIT
    cpu_quota: int = DEFAULT_WORKSPACE_CPU_QUOTA
    cpu_period: int = DEFAULT_WORKSPACE_CPU_PERIOD
    max_workspaces_per_user: int = DEFAULT_MAX_WORKSPACES_PER_USER
    startup_timeout: float = DEFAULT_OPENCODE_STARTUP_TIMEOUT

    def __post_init__(self) -> None:
        """校验工作区沙箱资源配额约束."""
        if not self.mem_limit.strip():
            msg = "mem_limit cannot be empty"
            raise ValueError(msg)
        if self.cpu_quota <= 0:
            msg = "cpu_quota must be > 0"
            raise ValueError(msg)
        if self.cpu_period <= 0:
            msg = "cpu_period must be > 0"
            raise ValueError(msg)
        if self.max_workspaces_per_user <= 0:
            msg = "max_workspaces_per_user must be > 0"
            raise ValueError(msg)
        if self.startup_timeout <= 0:
            msg = "startup_timeout must be > 0"
            raise ValueError(msg)


def get_runtime_llm_config() -> RuntimeLlmConfig:
    """从环境变量解析或使用默认运行时 LLM 配额配置."""
    max_out = _read_positive_int(
        "RUNTIME_LLM_MAX_OUTPUT_TOKENS", DEFAULT_RUNTIME_LLM_MAX_OUTPUT_TOKENS,
    )
    ctx = _read_positive_int(
        "RUNTIME_LLM_CONTEXT_TOKENS", DEFAULT_RUNTIME_LLM_CONTEXT_TOKENS,
    )
    if ctx <= max_out:
        ctx = max_out + 1
    return RuntimeLlmConfig(
        token_ttl_seconds=min(
            _read_positive_int(
                "RUNTIME_LLM_TOKEN_TTL_SECONDS",
                DEFAULT_RUNTIME_LLM_TOKEN_TTL_SECONDS,
            ),
            3600,
        ),
        hourly_token_budget=_read_positive_int(
            "RUNTIME_LLM_HOURLY_TOKEN_BUDGET",
            DEFAULT_RUNTIME_LLM_HOURLY_TOKEN_BUDGET,
        ),
        hourly_request_limit=_read_positive_int(
            "RUNTIME_LLM_HOURLY_REQUEST_LIMIT",
            DEFAULT_RUNTIME_LLM_HOURLY_REQUEST_LIMIT,
        ),
        max_output_tokens=max_out,
        context_tokens=ctx,
    )


def get_workspace_resource_config() -> WorkspaceResourceConfig:
    """从环境变量解析或使用默认沙箱资源限制配置."""
    raw_mem = os.getenv("WORKSPACE_MEM_LIMIT", DEFAULT_WORKSPACE_MEM_LIMIT)
    mem = (
        raw_mem.strip()
        if raw_mem and raw_mem.strip()
        else DEFAULT_WORKSPACE_MEM_LIMIT
    )
    return WorkspaceResourceConfig(
        mem_limit=mem,
        cpu_quota=_read_positive_int(
            "WORKSPACE_CPU_QUOTA", DEFAULT_WORKSPACE_CPU_QUOTA,
        ),
        cpu_period=_read_positive_int(
            "WORKSPACE_CPU_PERIOD", DEFAULT_WORKSPACE_CPU_PERIOD,
        ),
        max_workspaces_per_user=_read_positive_int(
            "MAX_WORKSPACES_PER_USER", DEFAULT_MAX_WORKSPACES_PER_USER,
        ),
        startup_timeout=_read_positive_float(
            "OPENCODE_STARTUP_TIMEOUT", DEFAULT_OPENCODE_STARTUP_TIMEOUT,
        ),
    )
