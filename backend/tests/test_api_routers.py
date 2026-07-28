"""API 层测试：dependencies + factory + routers 直接调用。"""

import pytest
import jwt
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi import HTTPException, Request

from app.api.dependencies import (
    get_current_user,
    require_admin,
    ensure_owner_or_admin,
    get_current_workspace,
    require_workspace_role,
)
from app.api.factory import create_fastapi_app, lifespan
from app.infra.extensions import SECRET_KEY
from app.models.user import User
from app.models.workspace import Workspace


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    async def test_no_token(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None, token=None, session=MagicMock())
        assert exc_info.value.status_code == 401

    async def test_valid_token(self, session, test_user):
        token = jwt.encode({"sub": str(test_user.id)}, SECRET_KEY, algorithm="HS256")
        user = await get_current_user(credentials=None, token=token, session=session)
        assert user.id == test_user.id

    async def test_expired_token(self):
        import datetime
        token = jwt.encode(
            {"sub": "1", "exp": datetime.datetime(2020, 1, 1)},
            SECRET_KEY, algorithm="HS256"
        )
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None, token=token, session=MagicMock())
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    async def test_invalid_token(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None, token="invalid.token.here", session=MagicMock())
        assert exc_info.value.status_code == 401

    async def test_no_sub(self):
        token = jwt.encode({"foo": "bar"}, SECRET_KEY, algorithm="HS256")
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None, token=token, session=MagicMock())
        assert exc_info.value.status_code == 401

    async def test_mcp_token_revoked(self, session, test_user):
        token = jwt.encode({"sub": str(test_user.id), "type": "mcp"}, SECRET_KEY, algorithm="HS256")
        with patch("app.mcp.token_service.get_active_mcp_token", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=None, token=token, session=session)
            assert exc_info.value.status_code == 401

    async def test_mcp_token_valid(self, session, test_user):
        token = jwt.encode({"sub": str(test_user.id), "type": "mcp"}, SECRET_KEY, algorithm="HS256")
        with patch("app.mcp.token_service.get_active_mcp_token", return_value=MagicMock()):
            user = await get_current_user(credentials=None, token=token, session=session)
        assert user.id == test_user.id

    async def test_bearer_credentials(self, session, test_user):
        token = jwt.encode({"sub": str(test_user.id)}, SECRET_KEY, algorithm="HS256")
        creds = MagicMock()
        creds.scheme = "Bearer"
        creds.credentials = token
        user = await get_current_user(credentials=creds, token=None, session=session)
        assert user.id == test_user.id


class TestRequireAdmin:
    def test_admin(self):
        user = MagicMock()
        user.role = "admin"
        assert require_admin(user) == user

    def test_non_admin(self):
        user = MagicMock()
        user.role = "user"
        with pytest.raises(HTTPException) as exc_info:
            require_admin(user)
        assert exc_info.value.status_code == 403


class TestEnsureOwnerOrAdmin:
    def test_owner(self):
        user = MagicMock()
        user.id = 1
        user.role = "user"
        ensure_owner_or_admin(user, 1)  # 不应报错

    def test_admin(self):
        user = MagicMock()
        user.id = 1
        user.role = "admin"
        ensure_owner_or_admin(user, 99)  # 不应报错

    def test_denied(self):
        user = MagicMock()
        user.id = 1
        user.role = "user"
        with pytest.raises(HTTPException) as exc_info:
            ensure_owner_or_admin(user, 99)
        assert exc_info.value.status_code == 403


class TestGetCurrentWorkspace:
    async def test_missing_header(self):
        request = MagicMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            await get_current_workspace(request, MagicMock(), MagicMock())
        assert exc_info.value.status_code == 400

    async def test_invalid_header(self):
        request = MagicMock()
        request.headers = {"X-Workspace-Id": "abc"}
        with pytest.raises(HTTPException) as exc_info:
            await get_current_workspace(request, MagicMock(), MagicMock())
        assert exc_info.value.status_code == 400

    async def test_not_found(self, session):
        request = MagicMock()
        request.headers = {"X-Workspace-Id": "99999"}
        user = MagicMock()
        user.id = 1
        with pytest.raises(HTTPException) as exc_info:
            await get_current_workspace(request, user, session)
        assert exc_info.value.status_code == 404

    async def test_success(self, session, test_workspace, test_user):
        request = MagicMock()
        request.headers = {"X-Workspace-Id": str(test_workspace.id)}
        ws = await get_current_workspace(request, test_user, session)
        assert ws.id == test_workspace.id


class TestRequireWorkspaceRole:
    async def test_sufficient_role(self, session, test_workspace, test_user):
        checker = require_workspace_role("viewer")
        with patch("app.features.workspace.permissions.get_member_role", return_value="admin"):
            result = await checker(workspace=test_workspace, current_user=test_user, session=session)
        assert result.id == test_workspace.id

    async def test_insufficient_role(self, session, test_workspace, test_user):
        checker = require_workspace_role("admin")
        with patch("app.features.workspace.permissions.get_member_role", return_value="viewer"):
            with pytest.raises(HTTPException) as exc_info:
                await checker(workspace=test_workspace, current_user=test_user, session=session)
            assert exc_info.value.status_code == 403

    async def test_no_role(self, session, test_workspace, test_user):
        checker = require_workspace_role("editor")
        with patch("app.features.workspace.permissions.get_member_role", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await checker(workspace=test_workspace, current_user=test_user, session=session)
            assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


class TestCreateFastapiApp:
    def test_creates_app(self):
        with patch("app.api.factory.initialize_application"):
            app = create_fastapi_app()
        assert app.title == "SKYCloud API"
        # 检查路由已注册
        routes = [r.path for r in app.routes]
        assert "/api/health" in routes


class TestLifespan:
    async def test_lifespan(self):
        with patch("app.api.factory.initialize_application") as m:
            async with lifespan(MagicMock()):
                pass
            m.assert_called_once()


# ---------------------------------------------------------------------------
# schemas 验证
# ---------------------------------------------------------------------------

from app.features.auth.schemas import LoginRequest, RegisterRequest
from app.features.file.schemas import FilePreflightRequest, MultipartInitRequest, BatchDeleteRequest
from app.features.workspace.schemas import WorkspaceCreateRequest
from app.features.share.schemas import ShareCreateRequest
from app.features.sys_dict.schemas import SysDictPayload
from app.features.chat.schemas import ChatRequest


class TestSchemas:
    def test_login_request(self):
        req = LoginRequest(username="user", password="pass")
        assert req.username == "user"

    def test_register_request(self):
        req = RegisterRequest(username="new", password="pass123")
        assert req.username == "new"

    def test_file_preflight(self):
        req = FilePreflightRequest(filename="f.txt", total_size=100, content_hash="a" * 64)
        assert req.filename == "f.txt"

    def test_multipart_init(self):
        req = MultipartInitRequest(filename="big.bin", total_size=1024)
        assert req.total_size == 1024

    def test_chat_request(self):
        req = ChatRequest(query="hello")
        assert req.query == "hello"

    def test_workspace_create(self):
        req = WorkspaceCreateRequest(name="ws")
        assert req.name == "ws"

    def test_share_create(self):
        req = ShareCreateRequest(file_id=1)
        assert req.file_id == 1

    def test_dict_create(self):
        req = SysDictPayload(key="k", value="v")
        assert req.key == "k"
