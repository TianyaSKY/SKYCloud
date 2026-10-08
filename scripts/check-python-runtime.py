"""Check real locked dependencies without pytest's optional-library mocks.

Only synthetic credentials and in-process ASGI transports are used. No database,
provider, storage, or user deployment is contacted.
"""
import asyncio
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'backend' if (root / 'backend').is_dir() else root))
os.environ.update(
    SECRET_KEY='disposable-locked-test-secret-32chars',
    OPENCODE_SERVER_SECRET='disposable-locked-server-secret-32chars',
    RUNTIME_CONTROLLER_TOKEN='disposable-locked-control-secret-32chars',
    RABBITMQ_PASSWORD='disposable-queue-secret', POSTGRES_PASSWORD='disposable-database-secret',
    MINIO_SECRET_KEY='disposable-storage-secret',
    DATABASE_URL='postgresql://test:disposable-database-secret@127.0.0.1:1/test',
    DEFAULT_MODEL_PWD='disposable-model-key', OPENCODE_IMAGE='fixture/runtime@sha256:' + 'a' * 64,
    APP_ORIGIN='https://cloud.example.test:8443', MCP_PORT='5001', OPENCODE_MCP_URL='',
)

import httpx
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from app.api.factory import create_fastapi_app
from app.mcp.server import mcp, get_mcp_app
from app.features.folder.organize.tools import get_all_files


async def check():
    llm = ChatOpenAI(api_key='disposable-model-key', base_url='http://127.0.0.1:1/v1', model='fixture')
    create_react_agent(llm, [get_all_files])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_fastapi_app()),
                                base_url='https://cloud.example.test:8443') as client:
        response = await client.get('/api/health')
        assert response.status_code == 200 and response.json() == {'success': True, 'data': {'status': 'ok'}}
    app = get_mcp_app(mcp)
    async with mcp.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='https://cloud.example.test:8443',
                                    headers={'Accept': 'application/json, text/event-stream',
                                             'Origin': 'https://cloud.example.test:8443'}) as client:
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                                  '_meta': {'fixture_padding': 'x' * (5 * 1024 * 1024)},
                                  'clientInfo': {'name': 'locked-smoke', 'version': '1'}}}
            response = await client.post('/mcp', json=request)
            assert response.status_code == 200 and 'serverInfo' in response.text
            client.headers['mcp-session-id'] = response.headers['mcp-session-id']
            response = await client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            assert response.status_code == 202
            response = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                                       'params': {'name': 'get_current_user', 'arguments': {}}})
            assert response.status_code == 200 and 'Unauthorized' in response.text
            response = await client.post('/mcp', json=request, headers={'Host': 'attacker.example.test'})
            assert response.status_code == 421
            response = await client.post('/mcp', json=request, headers={'Origin': 'https://attacker.example.test'})
            assert response.status_code == 403
    print('Real API, MCP protocol/auth/host/origin boundaries and organization-agent construction passed')


if __name__ == '__main__':
    asyncio.run(check())
