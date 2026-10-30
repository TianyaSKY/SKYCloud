"""Explicit SDK transport boundaries for the parent proxy and internal MCP host."""

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
    """Keyword options accepted by ``FastMCP`` for streamable HTTP."""

    transport_security: TransportSecuritySettings
    max_request_body_size: int


@dataclass(frozen=True)
class SafeOrigin:
    """Validated application/MCP origin with a non-empty hostname."""

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
    """Build SDK transport options with the existing Host/Origin allowlists."""
    hosts = set(_DEFAULT_ALLOWED_HOSTS)
    port = positive_limit("MCP_PORT", 8001)
    hosts.update({f"backend-mcp:{port}", f"skycloud-backend-mcp:{port}"})
    origins = set(_DEFAULT_ALLOWED_ORIGINS)
    for value in (
        os.getenv("APP_ORIGIN", "http://app.skycloud.localhost"),
        os.getenv("OPENCODE_MCP_URL"),
    ):
        if not value:
            continue
        origin = _safe_origin(value)
        # nginx forwards $host without the port; direct SDK clients send netloc.
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
