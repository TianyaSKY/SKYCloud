"""运行时秘密隔离、撤销、模型绑定与 HTTP 代理回归测试."""

from collections.abc import AsyncGenerator, Generator
from typing import Any, cast
from unittest.mock import Mock, patch

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from sqlalchemy.orm import Session, sessionmaker

from app.exceptions import register_exception_handlers
from app.features.auth.service import decode_token, generate_token
from app.features.runtime_llm import quota, tokens
from app.features.runtime_llm.router import router
from app.features.workspace import docker_service
from app.infra.extensions import SECRET_KEY, get_db, get_session_factory
from app.mcp import runtime_token_service
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


@pytest.fixture
def runtime(session: Session, test_user: User, test_workspace: Workspace) -> OpenCodeRuntime:
    runtime = OpenCodeRuntime(user_id=test_user.id, workspace_id=test_workspace.id, status="running")
    session.add(runtime)
    session.commit()
    return runtime


@pytest.fixture
def credential(session: Session, runtime: OpenCodeRuntime) -> str:
    return tokens.issue_token(session, runtime)


@pytest.fixture
def client(session: Session) -> Generator[TestClient, None, None]:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def body() -> dict[str, Any]:
    from app.infra.llm.config import get_chat_model_config
    return {"model": get_chat_model_config()["model"], "messages": [{"role": "user", "content": "hello"}]}


def auth(raw: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + raw}


def test_password_change_revokes_runtime_and_external_mcp_tokens(
    session: Session, test_user: User, runtime: OpenCodeRuntime
) -> None:
    from app.features.auth.user_service import ChangePasswordParams, change_password
    from app.mcp.token_service import ensure_user_mcp_token, get_active_mcp_token
    _mcp_record, external = ensure_user_mcp_token(session, test_user.id)
    _, scoped_mcp = runtime_token_service.issue_runtime_token(session, runtime, role="admin")
    credential = tokens.issue_token(session, runtime)
    assert tokens.validate_token(session, credential)
    change_password(session, ChangePasswordParams(actor_id=test_user.id, actor_role="common", user_id=test_user.id, old_password="password123", new_password="new-long-password"))
    assert external is not None
    assert get_active_mcp_token(session, external) is None
    assert tokens.validate_token(session, credential) is None
    assert runtime_token_service.validate_runtime_token(session, scoped_mcp) is None


def test_runtime_configuration_never_contains_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_API_KEY", "provider-secret-only-on-server")
    config = docker_service._chat_api_runtime_config("scoped-runtime-token")  # pyright: ignore[reportPrivateUsage]
    result = docker_service.merge_opencode_chat_provider({}, config)
    res = cast(dict[str, Any], result)
    assert res["provider"][docker_service.OPENCODE_CHAT_PROVIDER_ID]["options"]["apiKey"] == "scoped-runtime-token"
    assert "provider-secret-only-on-server" not in str(result)
    assert "5002/v1" in config["base_url"]


def test_token_is_hashed_bound_and_model_only(session: Session, credential: str, runtime: OpenCodeRuntime) -> None:
    from app.models.mcp_runtime_token import McpRuntimeToken
    payload = tokens.validate_token(session, credential)
    assert payload is not None
    assert payload["runtime_id"] == runtime.id
    assert payload["workspace_id"] == runtime.workspace_id
    assert payload["quota"] > 0
    exp = cast(int, payload.get("exp", 0))
    iat = cast(int, payload.get("iat", 0))
    assert exp - iat <= 3600
    record = session.query(McpRuntimeToken).filter_by(jti=payload["jti"]).one()
    assert record.token_hash != credential
    assert runtime_token_service.validate_runtime_token(session, credential) is None
    assert "Invalid" in decode_token(session, credential)


def test_revoked_stopped_and_demoted_credentials_fail(session: Session, credential: str, runtime: OpenCodeRuntime) -> None:
    assert tokens.validate_token(session, credential)
    runtime.status = "stopped"
    session.commit()
    assert tokens.validate_token(session, credential) is None
    runtime.status = "running"
    member = session.query(WorkspaceMember).filter_by(workspace_id=runtime.workspace_id, user_id=runtime.user_id).one()
    member.role = "viewer"
    session.commit()
    assert tokens.validate_token(session, credential) is None
    member.role = "editor"
    session.commit()
    runtime_token_service.revoke_runtime_tokens(session, runtime.id)
    session.commit()
    assert tokens.validate_token(session, credential) is None


def test_mcp_refresh_revokes_model_credential(session: Session, credential: str, runtime: OpenCodeRuntime) -> None:
    _, mcp = runtime_token_service.refresh_runtime_token(session, runtime, role="admin")
    assert tokens.validate_token(session, credential) is None
    new_llm = tokens.issue_token(session, runtime)
    assert tokens.validate_token(session, new_llm)
    assert runtime_token_service.validate_runtime_token(session, mcp)


def test_web_and_mcp_credentials_cannot_call_model_gateway(
    client: TestClient, session: Session, runtime: OpenCodeRuntime, test_user: User
) -> None:
    _, mcp = runtime_token_service.refresh_runtime_token(session, runtime, role="admin")
    web_token = generate_token(test_user.id)
    assert web_token is not None
    assert mcp is not None
    for raw in (web_token, mcp):
        assert client.post("/v1/chat/completions", headers=auth(raw), json=body()).status_code == 401


@pytest.mark.parametrize("change,status", [
    ({"model": "other"}, 403), ({"base_url": "http://db:5432"}, 400),
    ({"max_tokens": 999999}, 400), ({"stream": "yes"}, 400),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://internal"}}]}]}, 400),
])
def test_gateway_rejects_unsupported_requests(
    client: TestClient, credential: str, change: dict[str, Any], status: int
) -> None:
    with patch("app.features.runtime_llm.quota.reserve") as reserve:
        response = client.post("/v1/chat/completions", headers=auth(credential), json={**body(), **change})
    assert response.status_code == status
    reserve.assert_not_called()


def test_large_body_rejected(client: TestClient, credential: str) -> None:
    response = client.post("/v1/chat/completions", headers=auth(credential), json={**body(), "messages": [{"role": "user", "content": "a" * 300000}]})
    assert response.status_code == 413


def test_gateway_forwards_tools_and_uses_only_provider_auth(
    client: TestClient, credential: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_API_KEY", "real-provider-key")
    captured: dict[str, httpx.Request] = {}

    def provider(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    def upstream_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(provider))

    with patch("app.features.runtime_llm.router.upstream_client", upstream_client), patch("app.features.runtime_llm.quota.reserve") as reserve:
        response = client.post("/v1/chat/completions", headers=auth(credential), json=body())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "answer"
    assert captured["request"].headers["Authorization"] == "Bearer real-provider-key"
    assert credential not in str(captured["request"].headers)
    reserve.assert_called_once()


def test_gateway_stream_and_errors_do_not_leak_key(client: TestClient, credential: str) -> None:
    def upstream(response: httpx.Response) -> Any:
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))

    with patch("app.features.runtime_llm.quota.reserve"), patch("app.features.runtime_llm.router.upstream_client", upstream(httpx.Response(200, content=b'data: {"choices": []}\n\ndata: [DONE]\n\n'))):
        response = client.post("/v1/chat/completions", headers=auth(credential), json={**body(), "stream": True})
    assert response.status_code == 200
    assert "data: [DONE]" in response.text
    with patch("app.features.runtime_llm.quota.reserve"), patch("app.features.runtime_llm.router.upstream_client", upstream(httpx.Response(401, text="private-provider-key"))):
        response = client.post("/v1/chat/completions", headers=auth(credential), json=body())
    assert response.status_code == 502
    assert "private-provider-key" not in response.text


def test_quota_survives_token_refresh_and_fails_closed(credential: str) -> None:
    raw_payload: dict[str, Any] = jwt.decode(credential, SECRET_KEY, algorithms=["HS256"])
    payload = cast(tokens.RuntimeLlmClaims, raw_payload)
    with patch.object(quota.redis_client, "eval", return_value=1) as redis_eval:
        quota.reserve(payload, 100, 200)
        kwargs = redis_eval.call_args.args
        assert any(f"user:{payload['sub']}" in str(arg) for arg in kwargs)
        assert str(payload["jti"]) not in str(kwargs)
    from fastapi import HTTPException
    with patch.object(quota.redis_client, "eval", return_value=0), pytest.raises(HTTPException) as denied:
        quota.reserve(payload, 100, 200)
    assert denied.value.status_code == 429
    with patch.object(quota.redis_client, "eval", side_effect=ConnectionError), pytest.raises(HTTPException) as unavailable:
        quota.reserve(payload, 100, 200)
    assert unavailable.value.status_code == 503


def test_model_token_cannot_access_rest_or_mcp(session: Session, credential: str, runtime: OpenCodeRuntime) -> None:
    import asyncio
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from app.api.dependencies import get_current_user
    from app.mcp.server import JWTAuthMiddleware

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=credential)
    with pytest.raises(HTTPException) as denied:
        asyncio.run(get_current_user(credentials=credentials, session_factory=Mock()))
    assert denied.value.status_code == 403
    assert JWTAuthMiddleware._resolve_context(session, credential, str(runtime.workspace_id)) is None  # pyright: ignore[reportPrivateUsage]


def test_expired_or_tampered_model_token_is_rejected(session: Session, credential: str) -> None:
    import datetime as dt

    payload = jwt.decode(credential, SECRET_KEY, algorithms=["HS256"])
    payload["exp"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    expired = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    assert tokens.validate_token(session, expired) is None
    assert tokens.validate_token(session, credential + "tampered") is None


def test_runtime_model_output_limit_matches_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('RUNTIME_LLM_MAX_OUTPUT_TOKENS', '2048')
    monkeypatch.setenv('RUNTIME_LLM_CONTEXT_TOKENS', '32768')
    config = docker_service.merge_opencode_chat_provider({}, {'model': 'test-model', 'base_url': 'http://gateway/v1', 'api_key': 'scoped'})
    res = cast(dict[str, Any], config)
    assert res['provider'][docker_service.OPENCODE_CHAT_PROVIDER_ID]['models']['test-model']['limit'] == {'context': 32768, 'output': 2048}
    monkeypatch.setenv('RUNTIME_LLM_CONTEXT_TOKENS', '2048')
    with pytest.raises(ValueError, match='context must exceed'):
        docker_service.merge_opencode_chat_provider({}, {'model': 'test-model', 'base_url': 'http://gateway/v1', 'api_key': 'scoped'})


def test_continued_model_validation_does_not_touch_or_commit(
    session: Session, credential: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.mcp_runtime_token import McpRuntimeToken
    record = session.query(McpRuntimeToken).filter_by(token_hash=McpRuntimeToken.hash_token(credential)).one()
    before = record.last_used_at

    def fail_commit() -> None:
        pytest.fail('continued authorization must be read-only')

    monkeypatch.setattr(session, 'commit', fail_commit)
    assert tokens.validate_token(session, credential, touch=False)
    assert record.last_used_at == before and not session.dirty


@pytest.mark.parametrize('streaming', [False, True])
def test_response_limit_closes_guard_before_return(
    client: TestClient, credential: str, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    from app.features.runtime_llm import router as module
    closed: list[bool] = []

    async def guarded(*args: object) -> AsyncGenerator[bytes, None]:
        try:
            yield b'over-limit'
        finally:
            closed.append(True)

    monkeypatch.setattr(module, 'authorized_bytes', guarded)
    monkeypatch.setattr(module, 'MAX_RESPONSE_BYTES', 1)
    with patch('app.features.runtime_llm.quota.reserve'), patch.object(
        module, 'upstream_client', lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'ignored')))
    ):
        response = client.post('/v1/chat/completions', headers=auth(credential), json={**body(), 'stream': streaming})
    assert response.status_code == (200 if streaming else 502)
    assert closed == [True]
