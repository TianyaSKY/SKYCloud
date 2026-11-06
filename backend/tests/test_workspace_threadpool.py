"""缓慢的同步工作区/认证工作必须保持 ASGI 循环响应测试."""
import asyncio
import threading
from collections.abc import Callable
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_current_user, get_current_workspace, require_workspace_role
from app.features.auth import user_service
from app.features.workspace import router as workspace_router
from app.infra.extensions import get_db, get_session_factory
from app.models.user import User


@pytest.mark.parametrize(
    'boundary',
    ['create', 'workspace', 'role', 'profile', 'mcp', 'assistant_list', 'assistant_cancel'],
)
async def test_slow_sync_boundary_allows_concurrent_http_request(
    monkeypatch: pytest.MonkeyPatch, session: Session, boundary: str
) -> None:
    factory = sessionmaker(
        bind=session.get_bind(),
        join_transaction_mode='create_savepoint',
    )
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    user = SimpleNamespace(id=1)
    workspace = SimpleNamespace(id=2)

    def slow(result: Any) -> Callable[..., Any]:
        def call(*args: object, **kwargs: object) -> Any:
            # 若在事件循环上被调用则立即失败: 无死锁或仅依赖时序的断言.
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(5), 'test failed to release synchronous operation'
            return result

        return call

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: user
    mock_session = SimpleNamespace(get=slow(workspace))
    app.dependency_overrides[get_db] = lambda: mock_session
    app.dependency_overrides[get_session_factory] = lambda: lambda: nullcontext(mock_session)
    app.include_router(workspace_router.router)

    @app.get('/probe')
    async def probe() -> dict[str, bool]:
        assert not release.is_set()
        return {'responsive': True}

    method = 'GET'
    path = '/slow'
    kwargs: dict[str, Any] = {}

    if boundary in {'assistant_list', 'assistant_cancel'}:
        from app.features.assistant import router as assistant_router

        app.include_router(assistant_router.router)
        app.dependency_overrides[get_current_workspace] = lambda: workspace
        if boundary == 'assistant_list':
            monkeypatch.setattr(assistant_router.service, 'list_user_conversations', slow([]))
            method, path, kwargs = 'GET', '/assistant/conversations', {}
        else:
            monkeypatch.setattr(
                assistant_router.service,
                'request_cancel',
                slow(
                    SimpleNamespace(
                        to_dict=lambda: dict(
                            id=2,
                            conversation_id=1,
                            user_message_id=None,
                            assistant_message_id=None,
                            engine='fast',
                            status='cancelled',
                            request_id=None,
                            opencode_session_id=None,
                            pending_permission_id=None,
                            pending_permission=None,
                            cancel_requested=True,
                            error_code=None,
                            error_message=None,
                            metadata={},
                            started_at=None,
                            completed_at=None,
                            created_at=None,
                        )
                    )
                ),
            )

            async def notify_cancel(run_id: object) -> None:
                assert threading.get_ident() == loop_thread

            monkeypatch.setattr(assistant_router.service.run_registry, 'request_cancel', notify_cancel)
            method, path, kwargs = 'POST', '/assistant/runs/2/cancel', {}
    elif boundary == 'create':
        monkeypatch.setattr(
            workspace_router.workspace_service,
            'create_workspace',
            slow(
                SimpleNamespace(
                    to_dict=lambda: dict(
                        id=2,
                        name='Thread pool',
                        description=None,
                        owner_id=1,
                        created_at=None,
                        updated_at=None,
                        container_id=None,
                        status='stopped',
                        error_message=None,
                    )
                )
            ),
        )
        method, path, kwargs = 'POST', '/workspace', {'json': {'name': 'Thread pool'}}
    elif boundary == 'workspace':
        def _mock_assert_member(*args: object) -> None:
            pass

        monkeypatch.setattr('app.api.dependencies.assert_member', _mock_assert_member)

        @app.get('/slow')
        async def read_workspace(value: Any = Depends(get_current_workspace)) -> dict[str, object]:
            return {'id': value.id}

        method, path, kwargs = 'GET', '/slow', {'headers': {'X-Workspace-Id': '2'}}
    elif boundary == 'role':
        app.dependency_overrides[get_current_workspace] = lambda: workspace
        monkeypatch.setattr('app.features.workspace.permissions.get_member_role', slow('editor'))

        @app.get('/slow')
        async def read_role(
            value: Any = Depends(require_workspace_role('editor')),
        ) -> dict[str, object]:
            return {'id': value.id}

        method, path, kwargs = 'GET', '/slow', {}
    elif boundary == 'mcp':
        import jwt
        from fastapi.security import HTTPAuthorizationCredentials

        from app.infra.extensions import SECRET_KEY

        monkeypatch.setattr('app.mcp.token_service.get_active_mcp_token', slow(object()))

        def _mock_user_data(*args: object) -> dict[str, int]:
            return {'id': 1}

        def _mock_current_role(*args: object) -> str:
            return 'common'

        monkeypatch.setattr(user_service, 'get_user_data', _mock_user_data)
        monkeypatch.setattr('app.features.auth.account_repository.current_role', _mock_current_role)
        credentials = HTTPAuthorizationCredentials(
            scheme='Bearer',
            credentials=jwt.encode({'sub': '1', 'type': 'mcp'}, SECRET_KEY, algorithm='HS256'),
        )

        @app.get('/slow')
        async def read_mcp_user() -> dict[str, object]:
            value = await get_current_user(
                credentials=credentials,
                session_factory=factory,
            )
            return {'id': value.id}

        method, path, kwargs = 'GET', '/slow', {}
    else:
        def _mock_from_cache(data: object) -> SimpleNamespace:
            return user

        monkeypatch.setattr(user_service, 'get_user_data', slow({'id': 1}))
        monkeypatch.setattr(user_service.User, 'from_cache', _mock_from_cache)

        @app.get('/slow')
        async def read_profile() -> dict[str, object]:
            value = await user_service.get_user(cast(Any, mock_session), 1)
            return {'id': value.id}

        method, path, kwargs = 'GET', '/slow', {}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        request = asyncio.create_task(client.request(method, path, **kwargs))
        try:
            assert await asyncio.to_thread(entered.wait, 2), 'synchronous boundary did not start'
            response = await asyncio.wait_for(client.get('/probe'), timeout=1)
            assert response.json() == {'responsive': True}
            assert not request.done()
        finally:
            release.set()
            response = await request
        assert response.status_code in (200, 201), response.text


def test_http_create_workspace_preserves_description_and_validation(
    session: Session, test_user: User
) -> None:
    app = FastAPI()
    app.include_router(workspace_router.router)
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(
        bind=session.get_bind(), join_transaction_mode="create_savepoint"
    )
    with TestClient(app) as client:
        response = client.post(
            '/workspace', json={'name': '  Created over HTTP  ', 'description': 'Description'}
        )
        assert response.status_code == 201, response.text
        assert response.json()['data']['name'] == 'Created over HTTP'
        assert response.json()['data']['description'] == 'Description'
        assert client.post('/workspace', json={'name': 'Without description'}).status_code == 201
        assert client.post('/workspace', json={'name': '   '}).status_code == 422
        assert client.post('/workspace', json={'name': 'n' * 121}).status_code == 422
        assert client.post('/workspace', json={'name': 'Valid', 'description': 'd' * 513}).status_code == 422
