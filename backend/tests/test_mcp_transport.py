import pytest
from app.mcp.transport import transport_options


def test_transport_allows_configured_proxy_and_internal_host_without_wildcard_domains(monkeypatch):
    monkeypatch.setenv('APP_ORIGIN', 'https://cloud.example.com:8443')
    monkeypatch.setenv('OPENCODE_MCP_URL', 'http://private-mcp:5001/mcp')
    monkeypatch.setenv('MCP_PORT', '5001')
    monkeypatch.setenv('MCP_MAX_REQUEST_SIZE', '33554432')
    options = transport_options()
    security = options['transport_security']
    assert security['enable_dns_rebinding_protection'] is True
    assert {'cloud.example.com', 'cloud.example.com:8443', 'private-mcp:5001', 'skycloud-backend-mcp:5001'} <= set(security['allowed_hosts'])
    assert 'https://cloud.example.com:8443' in security['allowed_origins']
    assert all(host == 'localhost:*' or host == '127.0.0.1:*' or host == '[::1]:*' or '*' not in host
               for host in security['allowed_hosts'])
    assert options['max_request_body_size'] == 33554432


@pytest.mark.parametrize('url', ['https://user:secret@cloud.example.com', 'file:///tmp/mcp',
                               'https://cloud.example.com/mcp?token=secret'])
def test_transport_refuses_credential_bearing_or_unsafe_endpoint_configuration(monkeypatch, url):
    monkeypatch.setenv('OPENCODE_MCP_URL', url)
    with pytest.raises(ValueError, match='safe application/MCP origin'):
        transport_options()
