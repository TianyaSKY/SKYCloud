"""浏览器凭据范围、轮转、重放、撤销与故障行为测试."""

from collections.abc import Generator
from typing import Any, cast
from unittest.mock import patch

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_current_user
from app.features.auth import refresh_sessions as refresh
from app.features.auth.router import router
from app.infra.extensions import (
    SECRET_KEY,
    SessionFactory,
    get_db,
    get_session_factory,
)
from app.models.user import User


@pytest.fixture
def browser(
    session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("APP_ORIGIN", "http://app.skycloud.localhost")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: cast(
        SessionFactory,
        sessionmaker(
            bind=session.get_bind(), join_transaction_mode="create_savepoint"
        ),
    )

    @app.get("/api/protected")
    def protected(user: User = Depends(get_current_user)) -> dict[str, Any]:
        return {"id": user.id}

    with TestClient(app, base_url="http://app.skycloud.localhost") as client:
        yield client


HEADERS = {
    "Origin": "http://app.skycloud.localhost",
    "X-Skycloud-Session": "1",
}


def signin(browser: TestClient) -> Any:
    response = browser.post(
        "/api/auth/login",
        json={"username": "testuser", "password": "password123"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    return response


def test_login_short_access_hash_only_refresh_cookie_and_scope(
    browser: TestClient,
) -> None:
    response = signin(browser)
    raw = response.cookies[refresh.COOKIE]
    claims: dict[str, Any] = jwt.decode(
        response.json()["data"]["token"], SECRET_KEY, algorithms=["HS256"]
    )
    assert 899 <= claims["exp"] - claims["iat"] <= 900
    assert response.json()["data"]["expires_in"] == 900
    assert raw not in response.text and len(raw) == 43
    assert (
        "HttpOnly" in response.headers["set-cookie"]
        and "SameSite=strict" in response.headers["set-cookie"]
    )
    assert "Domain=" not in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    keys: list[str] = list(refresh.redis_client.scan_iter("web-auth:*"))  # pyright: ignore[reportUnknownMemberType]
    assert all(
        raw not in key and raw not in str(refresh.redis_client.get(key))  # pyright: ignore[reportUnknownMemberType]
        for key in keys
    )
    assert browser.get("/api/protected").status_code == 401
    assert (
        browser.get(
            "/api/protected", params={"token": response.json()["data"]["token"]}
        ).status_code
        == 401
    )


def test_refresh_rotation_and_replay_revoke_access_family(
    browser: TestClient,
) -> None:
    initial = signin(browser)
    previous = initial.cookies[refresh.COOKIE]
    renewed = browser.post("/api/auth/refresh", headers=HEADERS)
    assert renewed.status_code == 200
    assert renewed.cookies[refresh.COOKIE] != previous
    auth = {"Authorization": "Bearer " + renewed.json()["data"]["token"]}
    assert browser.get("/api/protected", headers=auth).status_code == 200
    browser.cookies.clear()
    replay = browser.post(
        "/api/auth/refresh",
        headers={**HEADERS, "Cookie": refresh.COOKIE + "=" + previous},
    )
    assert replay.status_code == 401 and "set-cookie" not in replay.headers
    assert browser.get("/api/protected", headers=auth).status_code == 401
    assert (
        browser.post(
            "/api/auth/refresh",
            headers={
                **HEADERS,
                "Cookie": refresh.COOKIE + "=" + renewed.cookies[refresh.COOKIE],
            },
        ).status_code
        == 401
    )


@pytest.mark.parametrize("path", ["refresh", "logout"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://evil.example", "X-Skycloud-Session": "1"},
        {"Origin": "null", "X-Skycloud-Session": "1"},
        {"Origin": "http://app.skycloud.localhost"},
    ],
)
def test_cookie_mutations_require_exact_origin_and_non_simple_header(
    browser: TestClient, path: str, headers: dict[str, str]
) -> None:
    initial = signin(browser)
    assert browser.post("/api/auth/" + path, headers=headers).status_code == 403
    assert (
        browser.get(
            "/api/protected",
            headers={"Authorization": "Bearer " + initial.json()["data"]["token"]},
        ).status_code
        == 200
    )


def test_logout_revokes_access_and_refresh_and_clears_cookie(
    browser: TestClient,
) -> None:
    initial = signin(browser)
    auth = {"Authorization": "Bearer " + initial.json()["data"]["token"]}
    response = browser.post("/api/auth/logout", headers=HEADERS)
    assert response.status_code == 200 and "Max-Age=0" in response.headers["set-cookie"]
    assert browser.get("/api/protected", headers=auth).status_code == 401
    assert (
        browser.post(
            "/api/auth/refresh",
            headers={
                **HEADERS,
                "Cookie": refresh.COOKIE + "=" + initial.cookies[refresh.COOKIE],
            },
        ).status_code
        == 401
    )


def test_logout_revokes_signed_access_family_when_cookie_is_missing(
    browser: TestClient,
) -> None:
    initial = signin(browser)
    browser.cookies.clear()
    auth = {"Authorization": "Bearer " + initial.json()["data"]["token"]}
    response = browser.post("/api/auth/logout", headers={**HEADERS, **auth})
    assert response.status_code == 200
    assert browser.get("/api/protected", headers=auth).status_code == 401


def test_password_version_change_invalidates_refresh_and_access(
    browser: TestClient, session: Session, test_user: User
) -> None:
    initial = signin(browser)
    session.execute(
        update(User).where(User.id == test_user.id).values(auth_version=1)
    )
    session.commit()
    assert browser.post("/api/auth/refresh", headers=HEADERS).status_code == 401
    assert (
        browser.get(
            "/api/protected",
            headers={"Authorization": "Bearer " + initial.json()["data"]["token"]},
        ).status_code
        == 401
    )


def test_current_role_and_profile_are_read_from_database_on_refresh(
    browser: TestClient, session: Session, test_user: User
) -> None:
    signin(browser)
    session.execute(
        update(User)
        .where(User.id == test_user.id)
        .values(role="admin", username="changed")
    )
    session.commit()
    response = browser.post("/api/auth/refresh", headers=HEADERS)
    assert response.status_code == 200
    assert (
        response.json()["data"]["role"] == "admin"
        and response.json()["data"]["user"]["username"] == "changed"
    )


def test_refresh_store_outage_fails_closed_without_rotating_cookie(
    browser: TestClient,
) -> None:
    signin(browser)
    with patch.object(
        refresh.redis_client, "get", side_effect=ConnectionError()
    ):
        response = browser.post("/api/auth/refresh", headers=HEADERS)
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert browser.post("/api/auth/refresh", headers=HEADERS).status_code == 200


def test_https_uses_secure_host_cookie(
    browser: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ORIGIN", "https://app.skycloud.localhost")
    response = browser.post(
        "/api/auth/login",
        json={"username": "testuser", "password": "password123"},
        headers={"Origin": "https://app.skycloud.localhost"},
    )
    assert response.status_code == 200
    assert "__Host-" + refresh.COOKIE in response.headers["set-cookie"]
    assert (
        "Secure" in response.headers["set-cookie"]
        and "Path=/" in response.headers["set-cookie"]
    )


@pytest.mark.asyncio
async def test_authoritative_session_check_does_not_block_event_loop(
    session: Session, test_user: User
) -> None:
    import asyncio
    import time
    from fastapi.security import HTTPAuthorizationCredentials
    from app.features.auth.service import generate_token

    started = time.monotonic()

    async def tick() -> float:
        await asyncio.sleep(0.02)
        return time.monotonic() - started

    def slow_check(*args: Any) -> bool:
        time.sleep(0.3)
        return True

    token = generate_token(int(test_user.id))
    assert token is not None

    with patch(
        "app.features.auth.sessions.valid_session", side_effect=slow_check
    ):
        factory = cast(
            SessionFactory,
            sessionmaker(
                bind=session.get_bind(), join_transaction_mode="create_savepoint"
            ),
        )
        _, elapsed = await asyncio.gather(
            get_current_user(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=token
                ),
                session_factory=factory,
            ),
            tick(),
        )
    assert elapsed < 0.15
