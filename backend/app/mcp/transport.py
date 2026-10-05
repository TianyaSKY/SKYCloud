"""Explicit SDK transport boundaries for the parent proxy and internal MCP host."""
import os
from urllib.parse import urlsplit

from app.infra.upload_limits import positive_limit


def transport_options():
    hosts = {'localhost', 'localhost:*', '127.0.0.1', '127.0.0.1:*', '[::1]', '[::1]:*',
             'backend-mcp', 'skycloud-backend-mcp'}
    port = positive_limit('MCP_PORT', 8001)
    hosts.update({f'backend-mcp:{port}', f'skycloud-backend-mcp:{port}'})
    origins = {'http://localhost:*', 'http://127.0.0.1:*', 'http://[::1]:*'}
    for value in [os.getenv('APP_ORIGIN', 'http://app.skycloud.localhost'), os.getenv('OPENCODE_MCP_URL')]:
        if not value:
            continue
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError('Configure a safe application/MCP origin')
        # nginx forwards $host without the port; direct SDK clients send netloc.
        hosts.update({parsed.netloc, f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname})
        origins.add(f'{parsed.scheme}://{parsed.netloc}')
    return {
        'transport_security': {
            'enable_dns_rebinding_protection': True,
            'allowed_hosts': sorted(hosts),
            'allowed_origins': sorted(origins),
        },
        'max_request_body_size': positive_limit('MCP_MAX_REQUEST_SIZE', 32 * 1024 * 1024),
    }
