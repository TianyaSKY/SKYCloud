"""不可用的身份存储拒绝访问且不假装凭据已过期测试."""

from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

from fastapi import Depends, FastAPI, HTTPException
import httpx
import jwt
import pytest
from redis.exceptions import ConnectionError
from sqlalchemy.orm import Session

from app.api import authentication, dependencies
from app.api.dependencies import SessionFactory
from app.exceptions import register_exception_handlers
from app.features.auth import refresh_sessions, sessions
from app.infra.extensions import SECRET_KEY, get_db, get_session_factory
from app.mcp import token_service


@pytest.mark.parametrize("kind", ["web", "mcp"])
@pytest.mark.parametrize("stage", ["open", "validate", "role", "profile", "close"])
async def test_auth_backend_fault_returns_unavailable_without_private_details(
    monkeypatch: pytest.MonkeyPatch, kind: str, stage: str,
) -> None:
    private = "private-database-and-credential-detail"

    def fault(*_args: Any) -> Any:
        raise RuntimeError(private)

    def ok(*_a: Any) -> Any:
        return True

    def role_ok(*_a: Any) -> str:
        return "common"

    def profile_ok(*_a: Any) -> dict[str, Any]:
        return {"id": 1, "role": "common"}

    @contextmanager
    def factory() -> Any:
        if stage == "open":
            fault()
        try:
            yield SimpleNamespace()
        finally:
            if stage == "close":
                fault()

    monkeypatch.setattr(
        sessions, "valid_session", fault if stage == "validate" else ok,
    )
    monkeypatch.setattr(
        token_service,
        "get_active_mcp_token",
        fault if stage == "validate" else ok,
    )
    monkeypatch.setattr(
        authentication.account_repository,
        "current_role",
        fault if stage == "role" else role_ok,
    )
    monkeypatch.setattr(
        authentication.user_service,
        "get_user_data",
        fault if stage == "profile" else profile_ok,
    )
    payload: dict[str, Any] = {"sub": "1", "ver": 0}
    if kind == "mcp":
        payload["type"] = "mcp"
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/protected")
    async def protected(_user: Any = Depends(dependencies.get_current_user)) -> None:
        pytest.fail("Unavailable authentication must not enter the endpoint")

    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_session_factory] = lambda: factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    ) as client:
        response = await client.get(
            "/protected",
            headers={
                "Authorization": "Bearer "
                + jwt.encode(payload, SECRET_KEY, algorithm="HS256"),
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert private not in response.text


async def test_browser_family_store_outage_propagates_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: Any) -> Any:
        raise ConnectionError("private-redis-credentials")

    monkeypatch.setattr(refresh_sessions.redis_client, "get", unavailable)
    payload: dict[str, Any] = {"sub": "1", "ver": 0, "sid": "a" * 43}
    raw = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    credentials = dependencies.HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=raw,
    )
    with pytest.raises(HTTPException) as error:
        await dependencies.get_current_user(
            credentials=credentials,
            session_factory=cast(SessionFactory, lambda: nullcontext(cast(Session, Mock()))),
        )
    assert error.value.status_code == 503


@pytest.mark.parametrize("sub", ["not-a-user", "0", "-1"])
@pytest.mark.parametrize("kind", ["web", "mcp"])
async def test_invalid_subject_stays_unauthorized_without_store_access(
    sub: str, kind: str,
) -> None:
    factory = Mock(side_effect=AssertionError("Malformed identity must not access SQL"))
    payload: dict[str, Any] = {"sub": sub, "ver": 0}
    if kind == "mcp":
        payload["type"] = "mcp"
    raw = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    credentials = dependencies.HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=raw,
    )
    with pytest.raises(HTTPException) as error:
        await dependencies.get_current_user(
            credentials=credentials,
            session_factory=cast(SessionFactory, factory),
        )
    assert error.value.status_code == 401
    factory.assert_not_called()
