"""Exercise security boundaries through actual HTTP dependency resolution."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.exceptions import BusinessRuleError, register_exception_handlers
from app.features.auth.router import router as auth_router
from app.features.auth.service import generate_token
from app.features.auth.user_service import update_user
from app.features.file.router import router as file_router
from app.infra.extensions import get_db, get_session_factory
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def client(session):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router, prefix="/api")
    app.include_router(file_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def headers(user):
    return {"Authorization": f"Bearer {generate_token(user.id, user.auth_version)}"}


def test_password_change_revokes_old_session_and_new_login_works(client, session, test_user):
    before = headers(test_user)
    assert client.get(f"/api/users/{test_user.id}", headers=before).status_code == 200
    changed = client.put(f"/api/users/{test_user.id}/password", headers=before,
                         json={"old_password": "password123", "new_password": "new-long-password"})
    assert changed.status_code == 200
    assert client.get(f"/api/users/{test_user.id}", headers=before).status_code == 401
    login = client.post("/api/auth/login", json={"username": test_user.username, "password": "new-long-password"})
    assert login.status_code == 200
    after = {"Authorization": "Bearer " + login.json()['data']["token"]}
    assert client.get(f"/api/users/{test_user.id}", headers=after).status_code == 200


def test_admin_password_reset_revokes_only_target_sessions(client, test_user, admin_user):
    victim = headers(test_user)
    administrator = headers(admin_user)
    changed = client.put(f"/api/users/{test_user.id}/password", headers=administrator,
                         json={"old_password": "ignored-by-admin", "new_password": "reset-long-password"})
    assert changed.status_code == 200
    assert client.get(f"/api/users/{test_user.id}", headers=victim).status_code == 401
    assert client.get(f"/api/users/{admin_user.id}", headers=administrator).status_code == 200


def test_failed_password_change_preserves_session(client, test_user):
    before = headers(test_user)
    changed = client.put(f"/api/users/{test_user.id}/password", headers=before,
                         json={"old_password": "wrong", "new_password": "new-long-password"})
    assert changed.status_code == 400
    assert client.get(f"/api/users/{test_user.id}", headers=before).status_code == 200


def test_unversioned_session_is_rejected(client, test_user):
    import jwt
    from app.infra.extensions import SECRET_KEY
    legacy = jwt.encode({"sub": str(test_user.id)}, SECRET_KEY, algorithm="HS256")
    assert client.get(f"/api/users/{test_user.id}", headers={"Authorization": "Bearer " + legacy}).status_code == 401


def test_mcp_token_is_revealed_only_by_explicit_issuance(client, session, test_user):
    from app.models.mcp_token import McpToken
    before = client.get("/api/auth/mcp-token", headers=headers(test_user))
    assert before.status_code == 200
    assert before.json()['data']["token"] is None
    assert "mcp_token" not in before.json()["data"]
    assert session.query(McpToken).count() == 0
    issued = client.post("/api/auth/mcp-token/refresh", headers=headers(test_user))
    assert issued.status_code == 200
    assert issued.headers["Cache-Control"] == "no-store"
    raw = issued.json()['data']["mcp_token"]
    record = session.query(McpToken).one()
    assert record.token_hash == McpToken.hash_token(raw)
    assert "token_value" not in McpToken.__table__.columns
    read = client.get("/api/auth/mcp-token", headers=headers(test_user))
    assert "mcp_token" not in read.json()["data"]
    assert raw not in read.text
    assert read.json()['data']["token"]["id"] == record.id
    assert session.query(McpToken).count() == 1


def test_login_does_not_rotate_or_issue_external_mcp_credentials(client, session, test_user):
    from app.models.mcp_token import McpToken
    login = client.post("/api/auth/login", json={"username": test_user.username, "password": "password123"})
    assert login.status_code == 200
    assert session.query(McpToken).count() == 0


@pytest.mark.parametrize("version", [None, True, -1, "0", 1])
def test_invalid_or_wrong_session_version_is_rejected(client, test_user, version):
    import jwt
    from app.infra.extensions import SECRET_KEY
    token = jwt.encode({"sub": str(test_user.id), "ver": version}, SECRET_KEY, algorithm="HS256")
    assert client.get(f"/api/users/{test_user.id}", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_cached_profile_cannot_restore_revoked_session(client, session, test_user):
    from sqlalchemy import update
    from app.features.auth.service import decode_token
    before = headers(test_user)
    session.execute(update(type(test_user)).where(type(test_user).id == test_user.id).values(auth_version=1))
    session.commit()
    with patch("app.features.auth.user_service._get_user_data", return_value=test_user.to_dict()):
        assert client.get(f"/api/users/{test_user.id}", headers=before).status_code == 401
        assert "revoked" in decode_token(session, before["Authorization"].split(" ", 1)[1]).lower()


def test_profile_update_cannot_change_password(client, session, test_user):
    response = client.put(
        f"/api/users/{test_user.id}",
        headers=headers(test_user),
        json={"username": "changed", "password": "attacker-password"},
    )
    assert response.status_code == 422
    session.refresh(test_user)
    assert test_user.username == "testuser"
    assert test_user.check_password("password123")


def test_service_rejects_credential_fields(session, test_user):
    with pytest.raises(BusinessRuleError):
        update_user(session, test_user.id, {"password_hash": "attacker-hash"})
    assert test_user.check_password("password123")


def test_management_user_creation_requires_admin(client, test_user, admin_user):
    body = {"username": "managed", "password": "secret"}
    assert client.post("/api/users", json=body).status_code == 401
    assert client.post("/api/users", headers=headers(test_user), json=body).status_code == 403
    response = client.post("/api/users", headers=headers(admin_user), json=body)
    assert response.status_code == 201
    assert response.json()['data']["role"] == "common"


def test_password_endpoint_still_requires_old_password(client, session, test_user):
    url = f"/api/users/{test_user.id}/password"
    response = client.put(url, headers=headers(test_user), json={"old_password": "wrong", "new_password": "new"})
    assert response.status_code == 400
    session.refresh(test_user)
    assert test_user.check_password("password123")
    response = client.put(url, headers=headers(test_user), json={"old_password": "password123", "new_password": "new"})
    assert response.status_code == 200
    session.refresh(test_user)
    assert test_user.check_password("new")


def test_upload_error_hides_internal_details(client, test_user, test_workspace):
    auth_headers = {**headers(test_user), "X-Workspace-Id": str(test_workspace.id)}
    secret = "/private/storage/internal-path database-password"
    with patch("app.features.file.service.create_uploaded_file", side_effect=RuntimeError(secret)):
        response = client.post("/api/files", headers=auth_headers, files={"file": ("test.txt", b"test")})
    assert response.status_code == 500
    assert secret not in response.text
    assert "Internal server error" in response.text


def test_workspace_list_uses_one_query(session, test_user, admin_user, test_workspace):
    from sqlalchemy import event
    from app.features.workspace.service import create_workspace, get_user_workspaces
    from app.models.workspace import WorkspaceMember

    second = create_workspace(session, test_user.id, "Second workspace")
    session.add(WorkspaceMember(workspace_id=second.id, user_id=admin_user.id, role="viewer"))
    session.commit()
    user_id, first_id, second_id = test_user.id, test_workspace.id, second.id
    queries = []
    engine = session.get_bind()

    def record_query(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", record_query)
    try:
        result = get_user_workspaces(session, user_id)
    finally:
        event.remove(engine, "before_cursor_execute", record_query)
    assert len(queries) == 1
    by_id = {row["id"]: row for row in result}
    assert by_id[first_id]["my_role"] == "admin"
    assert by_id[second_id]["my_role"] == "admin"
    assert by_id[second_id]["member_count"] == 2
    assert get_user_workspaces(session, 99999) == []


@pytest.mark.parametrize("kind", ["domain", "http"])
def test_server_errors_are_sanitized(kind):
    from fastapi import HTTPException
    from app.exceptions import ServiceOperationError

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/error")
    def error():
        if kind == "domain":
            raise ServiceOperationError("private-storage-password")
        raise HTTPException(status_code=503, detail="private-storage-password")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/error")
    assert response.status_code == (500 if kind == "domain" else 503)
    assert response.json() == {"success": False, "error": {"code": "INTERNAL_ERROR" if kind == "domain" else "SERVICE_UNAVAILABLE", "message": "Internal server error"}}


def test_registration_failure_hides_internal_error(client):
    with patch("app.features.auth.service.create_user", side_effect=RuntimeError("database-private-path-password")):
        response = client.post("/api/auth/register", json={"username": "register-error", "password": "secret"})
    assert response.status_code == 500
    assert "database-private-path-password" not in response.text
    assert response.json()["error"]["message"] == "Internal server error"


@pytest.mark.parametrize("kind", ["unexpected", "domain", "http"])
def test_mcp_server_errors_do_not_reveal_internal_details(kind):
    import json
    from fastapi import HTTPException
    from app.exceptions import ServiceOperationError
    from app.mcp.server import _service_error_json

    private = "postgresql://private-user:private-password@internal-db"
    errors = {
        "unexpected": RuntimeError(private),
        "domain": ServiceOperationError(private),
        "http": HTTPException(status_code=503, detail=private),
    }
    result = json.loads(_service_error_json(errors[kind]))
    assert result["error"] == "Service temporarily unavailable"
    assert private not in str(result)


async def test_vector_search_failure_is_sanitized(session, test_workspace):
    from app.features.file.service import search_files
    with patch("app.features.file.search_service.get_embedding_model_config", side_effect=RuntimeError("private-provider-key")):
        result = await search_files(session, test_workspace.id, "query", search_type="vector")
    assert result["error"] == "Vector search is temporarily unavailable"
    assert "private-provider-key" not in str(result)


def test_url_jwt_and_generic_cookies_never_authenticate(client, test_user, test_workspace):
    token = generate_token(test_user.id, test_user.auth_version)
    assert client.get(f'/api/users/{test_user.id}', params={'token': token}).status_code == 401
    assert client.get('/api/files/1/download', params={'token': token},
                      headers={'X-Workspace-Id': str(test_workspace.id)}).status_code == 401
    client.cookies.set('token', token)
    assert client.get(f'/api/users/{test_user.id}').status_code == 401
    assert client.get(f'/api/users/{test_user.id}', params={'token': 'invalid'},
                      headers=headers(test_user)).status_code == 200
