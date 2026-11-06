"""用于上级反向代理与内部 MCP 主机的显式 SDK 传输层边界."""

import os
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings

from app.infra.upload_limits import positive_limit

_DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
        "backend-mcp",
        "skycloud-backend-mcp",
    },
)
_DEFAULT_ALLOWED_ORIGINS = frozenset(
    {
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    },
)
_ALLOWED_ORIGIN_SCHEMES = frozenset({"http", "https"})
_UNSAFE_ORIGIN_MESSAGE = "Configure a safe application/MCP origin"


class TransportOptions(TypedDict):
    """``FastMCP`` 为流式 HTTP 接受的关键字配置选项."""

    transport_security: TransportSecuritySettings
    max_request_body_size: int


@dataclass(frozen=True)
class SafeOrigin:
    """经过校验且包含非空主机名的应用程序或 MCP Origin 来源."""

    scheme: str
    netloc: str
    hostname: str


def _safe_origin(value: str) -> SafeOrigin:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if (
        parsed.scheme not in _ALLOWED_ORIGIN_SCHEMES
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(_UNSAFE_ORIGIN_MESSAGE)
    return SafeOrigin(scheme=parsed.scheme, netloc=parsed.netloc, hostname=hostname)


def transport_options() -> TransportOptions:
    """基于现有 Host 与 Origin 白名单构建 SDK 传输层选项."""
    hosts = set(_DEFAULT_ALLOWED_HOSTS)
    port = positive_limit("MCP_PORT", 8001)
    hosts.update({f"backend-mcp:{port}", f"skycloud-backend-mcp:{port}"})
    origins = set(_DEFAULT_ALLOWED_ORIGINS)
    app_origin_val = os.getenv("APP_ORIGIN", "").strip()
    effective_app_origin = (
        app_origin_val
        if app_origin_val and app_origin_val != "*"
        else "http://app.skycloud.localhost"
    )
    for value in (
        effective_app_origin,
        os.getenv("OPENCODE_MCP_URL"),
    ):
        if not value:
            continue
        origin = _safe_origin(value)
        # nginx 转发不带端口的 $host; 直接 SDK 客户端发送包含端口的 netloc。
        hosts.update(
            {
                origin.netloc,
                f"[{origin.hostname}]" if ":" in origin.hostname else origin.hostname,
            },
        )
        origins.add(f"{origin.scheme}://{origin.netloc}")
    return {
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(hosts),
            allowed_origins=sorted(origins),
        ),
        "max_request_body_size": positive_limit(
            "MCP_MAX_REQUEST_SIZE",
            32 * 1024 * 1024,
        ),
    }
