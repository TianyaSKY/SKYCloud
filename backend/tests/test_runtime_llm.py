"""Runtime secret isolation, revocation, model binding and HTTP proxy regressions."""

from unittest.mock import patch

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.exceptions import register_exception_handlers
from app.features.auth.service import decode_token, generate_token
from app.features.runtime_llm import quota, tokens
from app.features.runtime_llm.router import router
from app.features.workspace import docker_service
from app.infra.extensions import SECRET_KEY, get_db
from app.mcp import runtime_token_service
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.workspace import WorkspaceMember


@pytest.fixture
def runtime(session, test_user, test_workspace):
    runtime = OpenCodeRuntime(user_id=test_user.id, workspace_id=test_workspace.id, status="running")
    session.add(runtime)
    session.commit()
    return runtime


@pytest.fixture
def credential(session, runtime):
    return tokens.issue_token(session, runtime)


@pytest.fixture
def client(session):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def body():
    from app.infra.llm.config import get_chat_model_config
    return {"model": get_chat_model_config()["model"], "messages": [{"role": "user", "content": "hello"}]}


def auth(raw):
    return {"Authorization": "Bearer " + raw}


def test_password_change_revokes_runtime_and_external_mcp_tokens(session, test_user, runtime):
    from app.features.auth.user_service import change_password
    from app.mcp.token_service import ensure_user_mcp_token, get_active_mcp_token
    mcp_record, external = ensure_user_mcp_token(session, test_user.id)
    _, scoped_mcp = runtime_token_service.issue_runtime_token(session, runtime, role="admin")
    credential = tokens.issue_token(session, runtime)
    assert tokens.validate_token(session, credential)
    change_password(session, test_user.id, "common", test_user.id, "password123", "new-long-password")
    assert get_active_mcp_token(session, external) is None
    assert tokens.validate_token(session, credential) is None
    assert runtime_token_service.validate_runtime_token(session, scoped_mcp) is None


def test_runtime_configuration_never_contains_provider_key(monkeypatch):
    monkeypatch.setenv("CHAT_API_KEY", "provider-secret-only-on-server")
    config = docker_service._chat_api_runtime_config("scoped-runtime-token")
    result = docker_service.merge_opencode_chat_provider({}, config)
    assert result["provider"][docker_service.OPENCODE_CHAT_PROVIDER_ID]["options"]["apiKey"] == "scoped-runtime-token"
    assert "provider-secret-only-on-server" not in str(result)
    assert "5002/v1" in config["base_url"]


def test_token_is_hashed_bound_and_model_only(session, credential, runtime):
    from app.models.mcp_runtime_token import McpRuntimeToken
    payload = tokens.validate_token(session, credential)
    assert payload["runtime_id"] == runtime.id
    assert payload["workspace_id"] == runtime.workspace_id
    assert payload["quota"] > 0
    assert payload["exp"] - payload["iat"] <= 3600
    record = session.query(McpRuntimeToken).filter_by(jti=payload["jti"]).one()
    assert record.token_hash != credential
    assert runtime_token_service.validate_runtime_token(session, credential) is None
    assert "Invalid" in decode_token(session, credential)


def test_revoked_stopped_and_demoted_credentials_fail(session, credential, runtime):
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


def test_mcp_refresh_revokes_model_credential(session, credential, runtime):
    _, mcp = runtime_token_service.refresh_runtime_token(session, runtime, role="admin")
    assert tokens.validate_token(session, credential) is None
    new_llm = tokens.issue_token(session, runtime)
    assert tokens.validate_token(session, new_llm)
    assert runtime_token_service.validate_runtime_token(session, mcp)


def test_web_and_mcp_credentials_cannot_call_model_gateway(client, session, runtime, test_user):
    _, mcp = runtime_token_service.refresh_runtime_token(session, runtime, role="admin")
    for raw in (generate_token(test_user.id), mcp):
        assert client.post("/v1/chat/completions", headers=auth(raw), json=body()).status_code == 401


@pytest.mark.parametrize("change,status", [
    ({"model": "other"}, 403), ({"base_url": "http://db:5432"}, 400),
    ({"max_tokens": 999999}, 400), ({"stream": "yes"}, 400),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://internal"}}]}]}, 400),
])
def test_gateway_rejects_unsupported_requests(client, credential, change, status):
    with patch("app.features.runtime_llm.quota.reserve") as reserve:
        response = client.post("/v1/chat/completions", headers=auth(credential), json={**body(), **change})
    assert response.status_code == status
    reserve.assert_not_called()


def test_large_body_rejected(client, credential):
    response = client.post("/v1/chat/completions", headers=auth(credential), json={**body(), "messages": [{"role": "user", "content": "a" * 300000}]})
    assert response.status_code == 413


def test_gateway_forwards_tools_and_uses_only_provider_auth(client, credential, monkeypatch):
    monkeypatch.setenv("CHAT_API_KEY", "real-provider-key")
    captured = {}

    def provider(request):
        captured["request"] = request
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    def upstream_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(provider))

    with patch("app.features.runtime_llm.router.upstream_client", upstream_client), patch("app.features.runtime_llm.quota.reserve") as reserve:
        response = client.post("/v1/chat/completions", headers=auth(credential), json=body())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "answer"
    assert captured["request"].headers["Authorization"] == "Bearer real-provider-key"
    assert credential not in str(captured["request"].headers)
    reserve.assert_called_once()


def test_gateway_stream_and_errors_do_not_leak_key(client, credential):
    def upstream(response):
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))

    with patch("app.features.runtime_llm.quota.reserve"), patch("app.features.runtime_llm.router.upstream_client", upstream(httpx.Response(200, content=b'data: {"choices": []}\n\ndata: [DONE]\n\n'))):
        response = client.post("/v1/chat/completions", headers=auth(credential), json={**body(), "stream": True})
    assert response.status_code == 200
    assert "data: [DONE]" in response.text
    with patch("app.features.runtime_llm.quota.reserve"), patch("app.features.runtime_llm.router.upstream_client", upstream(httpx.Response(401, text="private-provider-key"))):
        response = client.post("/v1/chat/completions", headers=auth(credential), json=body())
    assert response.status_code == 502
    assert "private-provider-key" not in response.text


def test_quota_survives_token_refresh_and_fails_closed(credential):
    payload = jwt.decode(credential, SECRET_KEY, algorithms=["HS256"])
    with patch.object(quota.redis_client, "eval", return_value=1) as redis_eval:
        quota.reserve(payload, 100, 200)
        kwargs = redis_eval.call_args.args
        assert any(f"user:{payload['sub']}" in str(arg) for arg in kwargs)
        assert payload["jti"] not in str(kwargs)
    from fastapi import HTTPException
    with patch.object(quota.redis_client, "eval", return_value=0), pytest.raises(HTTPException) as denied:
        quota.reserve(payload, 100, 200)
    assert denied.value.status_code == 429
    with patch.object(quota.redis_client, "eval", side_effect=ConnectionError), pytest.raises(HTTPException) as unavailable:
        quota.reserve(payload, 100, 200)
    assert unavailable.value.status_code == 503


def test_model_token_cannot_access_rest_or_mcp(session, credential, runtime):
    import asyncio
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from app.api.dependencies import get_current_user
    from app.mcp.server import JWTAuthMiddleware

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=credential)
    with pytest.raises(HTTPException) as denied:
        asyncio.run(get_current_user(credentials=credentials, session=session))
    assert denied.value.status_code == 403
    assert JWTAuthMiddleware._resolve_context(session, credential, str(runtime.workspace_id)) is None


def test_expired_or_tampered_model_token_is_rejected(session, credential):
    import datetime as dt

    payload = jwt.decode(credential, SECRET_KEY, algorithms=["HS256"])
    payload["exp"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    expired = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    assert tokens.validate_token(session, expired) is None
    assert tokens.validate_token(session, credential + "tampered") is None


def test_runtime_model_output_limit_matches_gateway(monkeypatch):
    monkeypatch.setenv('RUNTIME_LLM_MAX_OUTPUT_TOKENS', '2048')
    monkeypatch.setenv('RUNTIME_LLM_CONTEXT_TOKENS', '32768')
    config = docker_service.merge_opencode_chat_provider({}, {'model': 'test-model', 'base_url': 'http://gateway/v1', 'api_key': 'scoped'})
    assert config['provider'][docker_service.OPENCODE_CHAT_PROVIDER_ID]['models']['test-model']['limit'] == {'context': 32768, 'output': 2048}
    monkeypatch.setenv('RUNTIME_LLM_CONTEXT_TOKENS', '2048')
    with pytest.raises(ValueError, match='context must exceed'):
        docker_service.merge_opencode_chat_provider({}, {'model': 'test-model', 'base_url': 'http://gateway/v1', 'api_key': 'scoped'})
