"""认证模块测试：auth service / user_service。"""

from unittest.mock import patch, MagicMock

import jwt
import pytest

from app.infra.extensions import SECRET_KEY
from app.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.folder import Folder


# ---------------------------------------------------------------------------
# auth service
# ---------------------------------------------------------------------------


class TestGenerateToken:
    def test_generate_token_success(self):
        from app.features.auth.service import generate_token

        token = generate_token(42)
        assert token is not None
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        assert payload["sub"] == "42"

    def test_generate_token_has_expiry(self):
        from app.features.auth.service import generate_token

        token = generate_token(1)
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        assert "exp" in payload
        assert "iat" in payload


class TestGenerateMcpToken:
    def test_generate_mcp_token_success(self):
        from app.features.auth.service import generate_mcp_token

        token = generate_mcp_token(10)
        assert token is not None
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        assert payload["sub"] == "10"
        assert payload["type"] == "mcp"
        assert "jti" in payload

    def test_generate_mcp_token_custom_expiry(self):
        from datetime import datetime, timezone, timedelta
        from app.features.auth.service import generate_mcp_token

        custom_exp = datetime.now(timezone.utc) + timedelta(days=30)
        token = generate_mcp_token(5, expires_at=custom_exp)
        assert token is not None


class TestDecodeToken:
    def test_decode_valid_token(self, session):
        from app.features.auth.service import generate_token, decode_token

        token = generate_token(99)
        result = decode_token(session, token)
        assert result == "99"

    def test_decode_expired_token(self, session):
        from app.features.auth.service import decode_token
        import datetime as _dt

        payload = {
            "exp": _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1),
            "iat": _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2),
            "sub": "1",
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        result = decode_token(session, token)
        assert "expired" in result.lower()

    def test_decode_invalid_token(self, session):
        from app.features.auth.service import decode_token

        result = decode_token(session, "invalid.token.here")
        assert "invalid" in result.lower()


class TestAuthenticateUser:
    def test_success(self, session, test_user):
        from app.features.auth.service import authenticate_user

        token, role, user_id = authenticate_user(session, "testuser", "password123")
        assert token is not None
        assert role == "common"
        assert user_id == test_user.id

    def test_wrong_password(self, session, test_user):
        from app.features.auth.service import authenticate_user

        token, role, user_id = authenticate_user(session, "testuser", "wrong")
        assert token is None
        assert user_id is None

    def test_nonexistent_user(self, session):
        from app.features.auth.service import authenticate_user

        token, role, user_id = authenticate_user(session, "ghost", "pass")
        assert token is None


class TestLogin:
    def test_login_success(self, session, test_user):
        from app.features.auth.service import login

        with patch("app.features.auth.service.mcp_token_service") as mock_mcp:
            mock_mcp.ensure_user_mcp_token.return_value = (MagicMock(), "token")
            result = login(session, "testuser", "password123")

        assert result["token"] is not None
        assert result["role"] == "common"
        assert result["user_id"] == test_user.id

    def test_login_missing_credentials(self, session):
        from app.features.auth.service import login

        with pytest.raises(BusinessRuleError):
            login(session, "", "pass")

    def test_login_invalid_credentials(self, session, test_user):
        from app.features.auth.service import login

        with pytest.raises(AuthenticationError):
            login(session, "testuser", "wrongpass")


class TestRegisterUser:
    def test_register_success(self, session):
        from app.features.auth.service import register_user

        with patch("app.features.auth.service.mcp_token_service") as mock_mcp:
            mock_mcp.ensure_user_mcp_token.return_value = (MagicMock(), "token")
            user = register_user(session, "newuser", "pass123")

        assert user.username == "newuser"
        assert user.id is not None
        # 验证自动创建了工作空间
        ws = session.query(Workspace).filter_by(owner_id=user.id).first()
        assert ws is not None

    def test_register_missing_fields(self, session):
        from app.features.auth.service import register_user

        with pytest.raises(BusinessRuleError):
            register_user(session, "", "pass")


# ---------------------------------------------------------------------------
# user_service
# ---------------------------------------------------------------------------


class TestCreateUser:
    def test_create_user_with_workspace(self, session):
        from app.features.auth.user_service import create_user

        user = create_user(session, {"username": "alice", "password": "secret"})
        assert user.id is not None
        assert user.username == "alice"

        # 验证私人工作空间
        ws = session.query(Workspace).filter_by(owner_id=user.id).first()
        assert ws is not None
        assert "alice" in ws.name

        # 验证 admin 成员
        member = session.query(WorkspaceMember).filter_by(
            workspace_id=ws.id, user_id=user.id
        ).first()
        assert member.role == "admin"

        # 验证根文件夹
        root = session.query(Folder).filter_by(workspace_id=ws.id, parent_id=None).first()
        assert root is not None
        assert root.name == "/"


class TestGetUser:
    async def test_get_existing_user(self, session, test_user):
        from app.features.auth.user_service import get_user

        user = await get_user(session, test_user.id)
        assert user.id == test_user.id
        assert user.username == "testuser"

    async def test_get_nonexistent_user(self, session):
        from app.features.auth.user_service import get_user

        with pytest.raises(ResourceNotFoundError):
            await get_user(session, 99999)


class TestUpdateUser:
    def test_update_username(self, session, test_user):
        from app.features.auth.user_service import update_user

        updated = update_user(session, test_user.id, {"username": "newname"})
        assert updated.username == "newname"

    def test_update_nonexistent(self, session):
        from app.features.auth.user_service import update_user

        with pytest.raises(ResourceNotFoundError):
            update_user(session, 99999, {"username": "x"})


class TestDeleteUser:
    def test_delete_existing(self, session, test_user):
        from app.features.auth.user_service import delete_user

        user_id = test_user.id
        delete_user(session, user_id)
        assert session.get(User, user_id) is None

    def test_delete_nonexistent(self, session):
        from app.features.auth.user_service import delete_user

        with pytest.raises(ResourceNotFoundError):
            delete_user(session, 99999)


class TestChangePassword:
    def test_change_own_password(self, session, test_user):
        from app.features.auth.user_service import change_password

        change_password(
            session, test_user.id, "common", test_user.id, "password123", "newpass"
        )
        session.refresh(test_user)
        assert test_user.check_password("newpass")

    def test_admin_changes_other_password(self, session, test_user, admin_user):
        from app.features.auth.user_service import change_password

        change_password(
            session, admin_user.id, "admin", test_user.id, "", "adminset"
        )
        session.refresh(test_user)
        assert test_user.check_password("adminset")

    def test_non_admin_cannot_change_other(self, session, test_user):
        from app.features.auth.user_service import change_password

        other = User(username="other", role="common")
        other.set_password("otherpass")
        session.add(other)
        session.flush()

        with pytest.raises(PermissionDeniedError):
            change_password(session, other.id, "common", test_user.id, "x", "y")

    def test_wrong_old_password(self, session, test_user):
        from app.features.auth.user_service import change_password

        with pytest.raises(BusinessRuleError):
            change_password(
                session, test_user.id, "common", test_user.id, "wrongold", "new"
            )


class TestEnsureUserAccess:
    def test_self_access(self):
        from app.features.auth.user_service import ensure_user_access

        ensure_user_access(1, "common", 1)  # 不抛异常

    def test_admin_access(self):
        from app.features.auth.user_service import ensure_user_access

        ensure_user_access(1, "admin", 2)  # 不抛异常

    def test_denied_access(self):
        from app.features.auth.user_service import ensure_user_access

        with pytest.raises(PermissionDeniedError):
            ensure_user_access(1, "common", 2)
