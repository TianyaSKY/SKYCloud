"""带 SQL 状态与受控 RPC 的类型化工作区成功封套测试."""
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_current_user
from app.exceptions import register_exception_handlers
from app.features.workspace import router as workspace
from app.infra.extensions import get_db, get_session_factory
from app.models.user import User
from app.models.workspace import Workspace


@pytest.fixture
def client(
    session: Session, test_user: User, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(workspace.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(
        bind=session.get_bind(), join_transaction_mode="create_savepoint"
    )
    app.dependency_overrides[get_current_user] = lambda: test_user
    def _noop(*args: object) -> None:
        pass

    for name in ['start', 'stop', 'restart', 'setup_mcp']:
        monkeypatch.setattr(workspace.docker_service, name, _noop)

    def _summary(*args: object) -> dict[str, object]:
        return dict(
            runtime_id=None,
            user_id=test_user.id,
            config_version=None,
            access_url=None,
            container_id=None,
            status='stopped',
            error_message=None,
            private_token='private',
        )

    monkeypatch.setattr(workspace.docker_service, 'summary', _summary)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    'suffix',
    ['runtime/start', 'runtime/stop', 'runtime/restart', 'runtime/setup-mcp'],
)
def test_runtime_operations_use_typed_envelope(
    client: TestClient, test_workspace: Workspace, suffix: str
) -> None:
    response = client.post(f'/workspace/{test_workspace.id}/opencode/{suffix}')
    assert response.status_code == 200, response.text
    assert set(response.json()) == {'success', 'data'} and response.json()['success'] is True
    assert response.json()['data']['id'] == test_workspace.id
    assert 'private' not in response.text


def test_detail_and_member_crud_keep_current_roles(
    client: TestClient, test_workspace: Workspace, admin_user: User
) -> None:
    path = f'/workspace/{test_workspace.id}'
    detail = client.get(path)
    assert detail.status_code == 200, detail.text
    assert detail.json()['data']['my_role'] == 'admin'
    assert 'private' not in detail.text
    created = client.post(path + '/members', json={'username': admin_user.username, 'role': 'editor'})
    assert created.status_code == 201, created.text
    assert created.json()['data']['role'] == 'editor'
    changed = client.put(path + f'/members/{admin_user.id}', json={'role': 'viewer'})
    assert changed.status_code == 200 and changed.json()['data']['role'] == 'viewer'
    listed = client.get(path + '/members')
    assert listed.status_code == 200
    listed_data = cast(dict[str, Any], listed.json()['data'])
    assert {m['role'] for m in listed_data['members']} == {'admin', 'viewer'}
    removed = client.delete(path + f'/members/{admin_user.id}')
    assert removed.status_code == 200
    assert removed.json() == {'success': True, 'data': {'message': '已移除'}}


def test_invalid_response_returns_safe_canonical_error(
    client: TestClient, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _invalid(*args: object) -> dict[str, object]:
        return {'status': 'private-invalid-state'}

    monkeypatch.setattr(
        workspace.docker_service,
        'summary',
        _invalid,
    )
    response = client.get(f'/workspace/{test_workspace.id}/opencode/runtime')
    assert response.status_code == 500
    assert response.json()['error']['code'] == 'INTERNAL_ERROR'
    assert 'private' not in response.text


def test_all_workspace_openapi_successes_are_typed_envelopes(client: TestClient) -> None:
    schema = cast(dict[str, Any], client.get('/openapi.json').json())
    for operations in schema['paths'].values():
        for operation in operations.values():
            responses = operation['responses']
            code = '201' if '201' in responses else '200'
            ref = responses[code]['content']['application/json']['schema']['$ref']
            envelope = schema['components']['schemas'][ref.split('/')[-1]]
            assert envelope['properties']['success']['const'] is True
            assert '$ref' in envelope['properties']['data']
