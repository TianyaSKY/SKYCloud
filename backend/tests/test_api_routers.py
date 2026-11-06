"""API 层测试：dependencies + factory + routers 直接调用。"""
import datetime
from typing import Any, cast
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import (
    ensure_owner_or_admin,
    get_current_user,
    get_current_workspace,
    require_admin,
    require_workspace_role,
)
from app.api.factory import create_fastapi_app, lifespan
from app.features.auth.schemas import LoginRequest, RegisterRequest
from app.features.file.schemas import FilePreflightRequest, MultipartInitRequest
from app.features.share.schemas import ShareCreateRequest
from app.features.sys_dict.schemas import SysDictPayload
from app.features.workspace.schemas import WorkspaceCreateRequest
from app.infra.extensions import SECRET_KEY
from app.models.user import User
from app.models.workspace import Workspace


# dependencies


class TestGetCurrentUser:
    async def test_no_token(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None, session_factory=MagicMock())
        assert exc_info.value.status_code == 401

    async def test_valid_token(self, session: Session, test_user: User) -> None:
        token = jwt.encode({"sub": str(test_user.id), "ver": test_user.auth_version}, SECRET_KEY, algorithm="HS256")
        factory = sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
        user = await get_current_user(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            session_factory=factory,
        )
        assert user.id == test_user.id

    async def test_expired_token(self) -> None:
        token = jwt.encode(
            {"sub": "1", "exp": datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)},
            SECRET_KEY,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                session_factory=MagicMock(),
            )
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    async def test_invalid_token(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid.token.here"),
                session_factory=MagicMock(),
            )
        assert exc_info.value.status_code == 401

    async def test_no_sub(self) -> None:
        token = jwt.encode({"foo": "bar"}, SECRET_KEY, algorithm="HS256")
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                session_factory=MagicMock(),
            )
        assert exc_info.value.status_code == 401

    async def test_mcp_token_revoked(self, session: Session, test_user: User) -> None:
        token = jwt.encode({"sub": str(test_user.id), "type": "mcp"}, SECRET_KEY, algorithm="HS256")
        with patch("app.mcp.token_service.get_active_mcp_token", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                factory = sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
                await get_current_user(
                    credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                    session_factory=factory,
                )
            assert exc_info.value.status_code == 401

    async def test_mcp_token_valid(self, session: Session, test_user: User) -> None:
        token = jwt.encode({"sub": str(test_user.id), "type": "mcp"}, SECRET_KEY, algorithm="HS256")
        with patch("app.mcp.token_service.get_active_mcp_token", return_value=MagicMock()):
            factory = sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
            user = await get_current_user(
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                session_factory=factory,
            )
        assert user.id == test_user.id

    async def test_bearer_credentials(self, session: Session, test_user: User) -> None:
        token = jwt.encode({"sub": str(test_user.id), "ver": test_user.auth_version}, SECRET_KEY, algorithm="HS256")
        creds = MagicMock()
        creds.scheme = "Bearer"
        creds.credentials = token
        factory = sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
        user = await get_current_user(credentials=creds, session_factory=factory)
        assert user.id == test_user.id


class TestRequireAdmin:
    def test_admin(self) -> None:
        user = MagicMock()
        user.role = "admin"
        assert require_admin(user) == user

    def test_non_admin(self) -> None:
        user = MagicMock()
        user.role = "user"
        with pytest.raises(HTTPException) as exc_info:
            require_admin(user)
        assert exc_info.value.status_code == 403


class TestEnsureOwnerOrAdmin:
    def test_owner(self) -> None:
        user = MagicMock()
        user.id = 1
        user.role = "user"
        ensure_owner_or_admin(user, 1)  # 不应报错

    def test_admin(self) -> None:
        user = MagicMock()
        user.id = 1
        user.role = "admin"
        ensure_owner_or_admin(user, 99)  # 不应报错

    def test_denied(self) -> None:
        user = MagicMock()
        user.id = 1
        user.role = "user"
        with pytest.raises(HTTPException) as exc_info:
            ensure_owner_or_admin(user, 99)
        assert exc_info.value.status_code == 403


class TestGetCurrentWorkspace:
    async def test_missing_header(self) -> None:
        request = MagicMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            get_current_workspace(request, MagicMock(), MagicMock())
        assert exc_info.value.status_code == 400

    async def test_invalid_header(self) -> None:
        request = MagicMock()
        request.headers = {"X-Workspace-Id": "abc"}
        with pytest.raises(HTTPException) as exc_info:
            get_current_workspace(request, MagicMock(), MagicMock())
        assert exc_info.value.status_code == 400

    async def test_not_found(self, session: Session) -> None:
        request = MagicMock()
        request.headers = {"X-Workspace-Id": "99999"}
        user = MagicMock()
        user.id = 1
        with pytest.raises(HTTPException) as exc_info:
            get_current_workspace(request, user, lambda: session)
        assert exc_info.value.status_code == 404

    async def test_success(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        request = MagicMock()
        request.headers = {"X-Workspace-Id": str(test_workspace.id)}
        ws = get_current_workspace(request, test_user, lambda: session)
        assert ws.id == test_workspace.id


class TestRequireWorkspaceRole:
    async def test_sufficient_role(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        checker = require_workspace_role("viewer")
        with patch("app.features.workspace.permissions.get_member_role", return_value="admin"):
            result = checker(test_workspace, test_user, lambda: session)
        assert result.id == test_workspace.id

    async def test_insufficient_role(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        checker = require_workspace_role("admin")
        with patch("app.features.workspace.permissions.get_member_role", return_value="viewer"):
            with pytest.raises(HTTPException) as exc_info:
                checker(test_workspace, test_user, lambda: session)
            assert exc_info.value.status_code == 403

    async def test_no_role(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        checker = require_workspace_role("editor")
        with patch("app.features.workspace.permissions.get_member_role", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                checker(test_workspace, test_user, lambda: session)
            assert exc_info.value.status_code == 403


# factory


class TestCreateFastapiApp:
    def test_creates_app(self) -> None:
        with patch("app.api.factory.initialize_application"):
            app = create_fastapi_app()
            assert app.title == "SKYCloud API"
            # 检查公开路由行为; 包含的路由器可能是惰性加载的.
            with TestClient(app) as client:
                assert client.get('/api/health').json() == {'success': True, 'data': {'status': 'ok'}}


class TestLifespan:
    async def test_lifespan(self) -> None:
        with patch("app.api.factory.initialize_application") as m:
            async with lifespan(cast(Any, MagicMock())):
                pass
            m.assert_called_once()


# schemas 验证


class TestSchemas:
    def test_login_request(self) -> None:
        req = LoginRequest(username="user", password="pass")
        assert req.username == "user"

    def test_register_request(self) -> None:
        req = RegisterRequest(username="new", password="pass123")
        assert req.username == "new"

    def test_file_preflight(self) -> None:
        req = FilePreflightRequest(filename="f.txt", total_size=100, content_hash="a" * 64)
        assert req.filename == "f.txt"

    def test_multipart_init(self) -> None:
        req = MultipartInitRequest(filename="big.bin", total_size=1024)
        assert req.total_size == 1024

    def test_workspace_create(self) -> None:
        req = WorkspaceCreateRequest(name="ws", description=None)
        assert req.name == "ws"

    def test_share_create(self) -> None:
        req = ShareCreateRequest(file_id=1)
        assert req.file_id == 1

    def test_dict_create(self) -> None:
        req = SysDictPayload(key="k", value="v")
        assert req.key == "k"
