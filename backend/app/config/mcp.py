"""MCP (Model Context Protocol) 传输与请求限额强类型配置."""

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_MCP_PORT: Final[int] = 5001
DEFAULT_MCP_MAX_REQUEST_SIZE: Final[int] = 32 * 1024 * 1024  # 32 MiB
DEFAULT_MCP_REQUEST_CONCURRENCY: Final[int] = 4
DEFAULT_MCP_INLINE_UPLOAD_MAX_BYTES: Final[int] = 8 * 1024 * 1024  # 8 MiB


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except (TypeError, ValueError):
        return default
    else:
        return val if val > 0 else default


@dataclass(frozen=True, slots=True)
class McpConfig:
    """MCP 服务端口与安全防护配额."""

    port: int = DEFAULT_MCP_PORT
    max_request_size: int = DEFAULT_MCP_MAX_REQUEST_SIZE
    request_concurrency: int = DEFAULT_MCP_REQUEST_CONCURRENCY
    inline_upload_max_bytes: int = DEFAULT_MCP_INLINE_UPLOAD_MAX_BYTES

    def __post_init__(self) -> None:
        """校验 MCP 参数约束."""
        if self.port <= 0:
            msg = "port must be > 0"
            raise ValueError(msg)
        if self.max_request_size <= 0:
            msg = "max_request_size must be > 0"
            raise ValueError(msg)
        if self.request_concurrency <= 0:
            msg = "request_concurrency must be > 0"
            raise ValueError(msg)
        if self.inline_upload_max_bytes <= 0:
            msg = "inline_upload_max_bytes must be > 0"
            raise ValueError(msg)


def get_mcp_config() -> McpConfig:
    """从环境变量解析或使用默认 MCP 配置."""
    return McpConfig(
        port=_read_int("MCP_PORT", DEFAULT_MCP_PORT),
        max_request_size=_read_int(
            "MCP_MAX_REQUEST_SIZE", DEFAULT_MCP_MAX_REQUEST_SIZE,
        ),
        request_concurrency=_read_int(
            "MCP_REQUEST_CONCURRENCY", DEFAULT_MCP_REQUEST_CONCURRENCY,
        ),
        inline_upload_max_bytes=DEFAULT_MCP_INLINE_UPLOAD_MAX_BYTES,
    )
