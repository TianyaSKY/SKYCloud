"""HTTP 认证在其所属 worker 上解析 SQL 与缓存用户资料测试."""
import asyncio
import threading
from typing import Any, cast

import jwt
import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.api import authentication, dependencies
from app.features.auth import sessions
from app.infra.extensions import SECRET_KEY
from app.mcp import token_service


@pytest.mark.parametrize('kind', ['web', 'mcp'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cancelled_http_auth_drains_started_session(
    monkeypatch: pytest.MonkeyPatch, kind: str, fault: bool
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    closed: list[bool] = []

    class Owned:
        def __enter__(self) -> "Owned":
            return self

        def __exit__(self, *args: object) -> None:
            closed.append(True)

    def valid(*args: object) -> bool:
        entered.set()
        try:
            assert release.wait(5)
            if fault:
                raise RuntimeError('private-auth-error')
            return True
        finally:
            finished.set()

    def _mock_role(*args: object) -> str:
        return 'common'

    def _mock_user_data(*args: object) -> dict[str, object]:
        return {'id': 1, 'role': 'common'}

    monkeypatch.setattr(sessions, 'valid_session', valid)
    monkeypatch.setattr(token_service, 'get_active_mcp_token', valid)
    monkeypatch.setattr(authentication.account_repository, 'current_role', _mock_role)
    monkeypatch.setattr(authentication.user_service, 'get_user_data', _mock_user_data)
    payload: dict[str, object] = {'sub': '1', 'ver': 0}
    if kind == 'mcp':
        payload['type'] = 'mcp'
    credentials = HTTPAuthorizationCredentials(
        scheme='Bearer', credentials=jwt.encode(payload, SECRET_KEY, algorithm='HS256')
    )
    pending = asyncio.create_task(
        dependencies.get_current_user(
            credentials=credentials,
            session_factory=cast(Any, Owned),
        ),
    )
    error: pytest.ExceptionInfo[asyncio.CancelledError] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel('caller-stop')
        await asyncio.sleep(0.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(0.01)
        assert not pending.done() and not closed and not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as err:
            await asyncio.wait_for(pending, 2)
        error = err
        assert await asyncio.to_thread(finished.wait, 2)
    assert error is not None
    assert str(error.value) == 'caller-stop' and closed == [True]


@pytest.mark.parametrize('kind', ['web', 'mcp'])
async def test_authentication_worker_ownership_and_fresh_role(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened: list[Any] = []
    closed: list[Any] = []
    profile: dict[str, object] = {'id': 1, 'role': 'admin'}

    class OwnedSession:
        def __init__(self) -> None:
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            opened.append(self)

        def __enter__(self) -> "OwnedSession":
            return self

        def __exit__(self, *args: object) -> None:
            assert threading.get_ident() == self.owner
            closed.append(self)

    def valid(db: Any, *args: object) -> bool:
        assert threading.get_ident() == db.owner
        entered.set()
        assert release.wait(5)
        return True

    def role(db: Any, *args: object) -> str:
        assert threading.get_ident() == db.owner
        return 'common'

    def cached(db: Any, *args: object) -> dict[str, object]:
        assert threading.get_ident() == db.owner
        return profile

    monkeypatch.setattr(sessions, 'valid_session', valid)
    monkeypatch.setattr(token_service, 'get_active_mcp_token', valid)
    monkeypatch.setattr(authentication.account_repository, 'current_role', role)
    monkeypatch.setattr(authentication.user_service, 'get_user_data', cached)
    payload: dict[str, object] = {'sub': '1', 'ver': 0}
    if kind == 'mcp':
        payload['type'] = 'mcp'
    credential = HTTPAuthorizationCredentials(
        scheme='Bearer', credentials=jwt.encode(payload, SECRET_KEY, algorithm='HS256')
    )
    pending = asyncio.create_task(
        dependencies.get_current_user(
            credentials=credential,
            session_factory=cast(Any, OwnedSession),
        ),
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)

        async def probe() -> bool:
            assert not release.is_set()
            return True

        assert await asyncio.wait_for(probe(), 1) and not pending.done()
    finally:
        release.set()
        user = await pending
    assert user.id == 1 and user.role == 'common'
    user_any = cast(Any, user)
    assert user_any.http_stream_credential.token == credential.credentials
    assert credential.credentials not in repr(user_any.http_stream_credential)
    assert 'http_stream_credential' not in user.to_dict()
    assert profile == {'id': 1, 'role': 'admin'}
    assert user_any._sa_instance_state.session is None
    assert profile['role'] == 'admin'
    assert len(opened) == 1 and closed == opened
