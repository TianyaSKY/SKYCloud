"""auth/share/sys_dict/dependencies 单元测试。

这些模块的函数签名与常规不同，需特别注意：
- authenticate_user 返回元组 (token, role, user_id)
- generate_token(user_id) 接收 int
- sys_dict service 使用 db.session（Flask 风格），不接受 session 参数
- get_current_user 接收 credentials, token, session 三个参数
- get_share_by_token 而非 get_share_link_by_token
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAuthService:
    def test_generate_token(self):
        from app.features.auth.service import generate_token
        token = generate_token(1)
        assert token is not None
        assert isinstance(token, str)

    def test_generate_token_error(self):
        from app.features.auth.service import generate_token
        with patch("app.features.auth.service.jwt.encode", side_effect=Exception("JWT error")):
            token = generate_token(1)
        assert token is None

    def test_generate_mcp_token(self):
        from app.features.auth.service import generate_mcp_token
        token = generate_mcp_token(1)
        assert token is not None
        assert isinstance(token, str)

    def test_generate_mcp_token_error(self):
        from app.features.auth.service import generate_mcp_token
        with patch("app.features.auth.service.jwt.encode", side_effect=Exception("JWT error")):
            token = generate_mcp_token(1)
        assert token is None

    def test_decode_token_valid(self, session, test_user):
        from app.features.auth.service import decode_token, generate_token
        token = generate_token(test_user.id)
        result = decode_token(session, token)
        assert result == str(test_user.id)

    def test_decode_token_expired(self, session):
        from app.features.auth.service import decode_token
        import jwt
        from app.infra.extensions import SECRET_KEY
        # Create an expired token
        import datetime
        payload = {
            "exp": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1),
            "sub": "1",
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        result = decode_token(session, token)
        assert "expired" in result.lower()

    def test_decode_token_invalid(self, session):
        from app.features.auth.service import decode_token
        result = decode_token(session, "invalid.token.here")
        assert "Invalid" in result or "invalid" in result.lower()

    def test_decode_token_mcp_revoked(self, session, test_user):
        from app.features.auth.service import decode_token, generate_mcp_token
        from app.mcp import token_service
        token = generate_mcp_token(test_user.id)
        with patch.object(token_service, "get_active_mcp_token", return_value=None):
            result = decode_token(session, token)
        assert "revoked" in result.lower() or "expired" in result.lower()

    def test_decode_token_mcp_active(self, session, test_user):
        from app.features.auth.service import decode_token, generate_mcp_token
        from app.mcp import token_service
        token = generate_mcp_token(test_user.id)
        mock_token = MagicMock()
        with patch.object(token_service, "get_active_mcp_token", return_value=mock_token):
            result = decode_token(session, token)
        assert result == str(test_user.id)

    def test_authenticate_user_success(self, session, test_user):
        from app.features.auth.service import authenticate_user
        token, role, user_id = authenticate_user(session, "testuser", "password123")
        assert token is not None
        assert role == "common"
        assert user_id == test_user.id

    def test_authenticate_user_wrong_password(self, session, test_user):
        from app.features.auth.service import authenticate_user
        token, role, user_id = authenticate_user(session, "testuser", "wrong")
        assert token is None
        assert role == "common"
        assert user_id is None

    def test_authenticate_user_not_found(self, session):
        from app.features.auth.service import authenticate_user
        token, role, user_id = authenticate_user(session, "nobody", "pass")
        assert token is None

    def test_login_success(self, session, test_user):
        from app.features.auth.service import login
        with patch("app.features.auth.service.mcp_token_service.ensure_user_mcp_token"):
            result = login(session, "testuser", "password123")
        assert "token" in result
        assert result["role"] == "common"

    def test_login_no_credentials(self, session):
        from app.features.auth.service import login
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            login(session, "", "")

    def test_login_invalid(self, session, test_user):
        from app.features.auth.service import login
        from app.exceptions import AuthenticationError
        with patch("app.features.auth.service.mcp_token_service.ensure_user_mcp_token"):
            with pytest.raises(AuthenticationError):
                login(session, "testuser", "wrong")

    def test_login_mcp_init_failure(self, session, test_user):
        """MCP token 初始化失败不影响登录。"""
        from app.features.auth.service import login
        # logger.exception 使用 {} 格式，标准 logging 使用 % 格式会报 TypeError
        with patch("app.features.auth.service.mcp_token_service.ensure_user_mcp_token",
                   side_effect=Exception("MCP init failed")), \
             patch("app.features.auth.service.logger"):
            result = login(session, "testuser", "password123")
        assert "token" in result

    def test_register_user_success(self, session):
        from app.features.auth.service import register_user
        with patch("app.features.auth.service.mcp_token_service.ensure_user_mcp_token"):
            user = register_user(session, "newuser", "newpass")
        assert user.username == "newuser"

    def test_register_user_no_credentials(self, session):
        from app.features.auth.service import register_user
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            register_user(session, "", "")

    def test_register_user_duplicate(self, session, test_user):
        from app.features.auth.service import register_user
        from app.exceptions import BusinessRuleError
        with patch("app.features.auth.service.mcp_token_service.ensure_user_mcp_token"), \
             patch("app.features.auth.service.logger"):
            with pytest.raises(BusinessRuleError):
                register_user(session, "testuser", "pass")

    def test_register_user_mcp_fail(self, session):
        from app.features.auth.service import register_user
        with patch("app.features.auth.service.mcp_token_service.ensure_user_mcp_token",
                   side_effect=Exception("MCP init failed")), \
             patch("app.features.auth.service.logger"):
            user = register_user(session, "mcpfail", "pass")
        assert user.username == "mcpfail"

    def test_get_mcp_token(self, session, test_user):
        from app.features.auth.service import get_mcp_token
        mock_payload = {"token": "abc"}
        with patch("app.features.auth.service.mcp_token_service.get_user_mcp_token_payload",
                   return_value=mock_payload):
            result = get_mcp_token(session, test_user.id)
        assert result == mock_payload

    def test_refresh_mcp_token(self, session, test_user):
        from app.features.auth.service import refresh_mcp_token
        mock_record = MagicMock()
        mock_record.to_dict.return_value = {"id": 1}
        with patch("app.features.auth.service.mcp_token_service.refresh_user_mcp_token",
                   return_value=(mock_record, "raw_token")):
            result = refresh_mcp_token(session, test_user.id)
        assert result["mcp_token"] == "raw_token"

    def test_issue_mcp_token_alias(self, session, test_user):
        from app.features.auth.service import issue_mcp_token, refresh_mcp_token
        assert issue_mcp_token is not None


class TestShareService:
    def test_create_share_link(self, session, test_workspace, test_user):
        from app.features.share.service import create_share_link
        from app.models.file import File
        f = File(name="share.txt", file_path="sh.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=test_user.id, parent_id=None,
                 content_hash="s" * 64, status="success")
        session.add(f)
        session.flush()
        share = create_share_link(session, test_user.id, f.id)
        assert share.user_id == test_user.id
        assert share.file_id == f.id

    def test_create_share_link_file_not_found(self, session, test_user):
        from app.features.share.service import create_share_link
        with pytest.raises(ValueError, match="File not found"):
            create_share_link(session, test_user.id, 99999)

    def test_get_share_by_token_not_found(self, session):
        from app.features.share.service import get_share_by_token
        assert get_share_by_token(session, "nonexistent") is None

    def test_get_share_by_token_found(self, session, test_workspace, test_user):
        from app.features.share.service import get_share_by_token, create_share_link
        from app.models.file import File
        f = File(name="share2.txt", file_path="sh2.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=test_user.id, parent_id=None,
                 content_hash="t" * 64, status="success")
        session.add(f)
        session.flush()
        share = create_share_link(session, test_user.id, f.id)
        result = get_share_by_token(session, share.token)
        assert result is not None
        assert result.id == share.id

    def test_get_share_by_token_expired(self, session, test_workspace, test_user):
        from app.features.share.service import get_share_by_token, create_share_link
        from app.models.file import File
        from datetime import datetime, timedelta
        f = File(name="share3.txt", file_path="sh3.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=test_user.id, parent_id=None,
                 content_hash="u" * 64, status="success")
        session.add(f)
        session.flush()
        expired = datetime.now() - timedelta(days=1)
        share = create_share_link(session, test_user.id, f.id, expires_at=expired)
        result = get_share_by_token(session, share.token)
        assert result is None

    def test_get_my_shares(self, session, test_workspace, test_user):
        from app.features.share.service import get_my_shares, create_share_link
        from app.models.file import File
        f = File(name="myshare.txt", file_path="ms.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=test_user.id, parent_id=None,
                 content_hash="v" * 64, status="success")
        session.add(f)
        session.flush()
        create_share_link(session, test_user.id, f.id)
        result = get_my_shares(session, test_user.id)
        assert len(result) >= 1

    def test_cancel_share_success(self, session, test_workspace, test_user):
        from app.features.share.service import cancel_share, create_share_link
        from app.models.file import File
        f = File(name="cancel.txt", file_path="ca.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=test_user.id, parent_id=None,
                 content_hash="w" * 64, status="success")
        session.add(f)
        session.flush()
        share = create_share_link(session, test_user.id, f.id)
        assert cancel_share(session, share.id, test_user.id) is True

    def test_cancel_share_not_found(self, session, test_user):
        from app.features.share.service import cancel_share
        assert cancel_share(session, 99999, test_user.id) is False

    def test_create_share_bad_date(self, session, test_workspace, test_user):
        from app.features.share.service import create_share
        from app.models.file import File
        from app.exceptions import BusinessRuleError
        f = File(name="bad.txt", file_path="bd.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=test_user.id, parent_id=None,
                 content_hash="x" * 64, status="success")
        session.add(f)
        session.flush()
        with pytest.raises(BusinessRuleError, match="date format"):
            create_share(session, test_user.id, f.id, "not-a-date")

    def test_create_share_file_not_found(self, session, test_user):
        from app.features.share.service import create_share
        from app.exceptions import ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError):
            create_share(session, test_user.id, 99999, None)

    def test_cancel_share_for_user_not_found(self, session, test_user):
        from app.features.share.service import cancel_share_for_user
        from app.exceptions import ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError):
            cancel_share_for_user(session, 99999, test_user.id)

    def test_resolve_shared_file_not_found(self, session):
        from app.features.share.service import resolve_shared_file
        from app.exceptions import ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError, match="Link invalid"):
            resolve_shared_file(session, "nonexistent")


class TestSysDictService:
    def test_create_sys_dict(self, session, test_user):
        from app.features.sys_dict.service import create_sys_dict
        with patch("app.features.sys_dict.service.db") as mock_db:
            mock_db.session = session
            with patch("app.features.sys_dict.service._invalidate_sys_dict_cache"):
                result = create_sys_dict({"key": "test_key", "value": "val", "des": "desc", "enable": True})
        assert result.key == "test_key"

    def test_create_sys_dict_model_config_key(self):
        from app.features.sys_dict.service import create_sys_dict
        from app.exceptions import BusinessRuleError
        with patch("app.features.sys_dict.service.is_model_config_sys_dict_key", return_value=True):
            with pytest.raises(BusinessRuleError):
                create_sys_dict({"key": "CHAT_MODEL_API", "value": "x"})

    def test_update_sys_dict(self, session, test_user):
        from app.features.sys_dict.service import update_sys_dict
        from app.models.sys_dict import SysDict
        sd = SysDict(key="update_me", value="old", des="d", enable=True)
        session.add(sd)
        session.flush()
        with patch("app.features.sys_dict.service.db") as mock_db:
            mock_db.session = session
            with patch("app.features.sys_dict.service._invalidate_sys_dict_cache"):
                result = update_sys_dict(sd.id, {"value": "new"})
        assert result.value == "new"

    def test_update_sys_dict_not_found(self, session):
        from app.features.sys_dict.service import update_sys_dict
        from app.exceptions import ResourceNotFoundError
        with patch("app.features.sys_dict.service.db") as mock_db:
            mock_db.session = session
            with pytest.raises(ResourceNotFoundError):
                update_sys_dict(99999, {"value": "x"})

    def test_update_sys_dict_model_config_key(self, session):
        from app.features.sys_dict.service import update_sys_dict
        from app.models.sys_dict import SysDict
        from app.exceptions import BusinessRuleError
        sd = SysDict(key="normal_key", value="old", des="d", enable=True)
        session.add(sd)
        session.flush()
        with patch("app.features.sys_dict.service.db") as mock_db:
            mock_db.session = session
            with patch("app.features.sys_dict.service.is_model_config_sys_dict_key", return_value=True):
                with pytest.raises(BusinessRuleError):
                    update_sys_dict(sd.id, {"key": "CHAT_MODEL_API"})

    def test_delete_sys_dict(self, session):
        from app.features.sys_dict.service import delete_sys_dict
        from app.models.sys_dict import SysDict
        sd = SysDict(key="delete_me", value="v", des="d", enable=True)
        session.add(sd)
        session.flush()
        with patch("app.features.sys_dict.service.db") as mock_db:
            mock_db.session = session
            with patch("app.features.sys_dict.service._invalidate_sys_dict_cache"):
                delete_sys_dict(sd.id)

    def test_delete_sys_dict_not_found(self, session):
        from app.features.sys_dict.service import delete_sys_dict
        from app.exceptions import ResourceNotFoundError
        with patch("app.features.sys_dict.service.db") as mock_db:
            mock_db.session = session
            with pytest.raises(ResourceNotFoundError):
                delete_sys_dict(99999)

    def test_get_sys_dict_all(self, session):
        from app.features.sys_dict.service import get_sys_dict_all
        from app.models.sys_dict import SysDict
        sd = SysDict(key="list_me", value="v", des="d", enable=True)
        session.add(sd)
        session.flush()
        # @cacheable 使用 Redis，mock redis 返回 None（cache miss）
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("app.features.sys_dict.service.db") as mock_db, \
             patch("app.infra.cache.redis_client", mock_redis):
            mock_db.session = session
            result = asyncio.run(get_sys_dict_all())
        assert any(d["key"] == "list_me" for d in result)

    def test_get_sys_dict_by_key(self, session):
        from app.features.sys_dict.service import get_sys_dict_by_key
        from app.models.sys_dict import SysDict
        sd = SysDict(key="find_me", value="v", des="d", enable=True)
        session.add(sd)
        session.flush()
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("app.features.sys_dict.service.db") as mock_db, \
             patch("app.infra.cache.redis_client", mock_redis):
            mock_db.session = session
            result = asyncio.run(get_sys_dict_by_key("find_me"))
        assert result is not None

    def test_get_sys_dict_by_key_not_found(self, session):
        from app.features.sys_dict.service import get_sys_dict_by_key
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("app.features.sys_dict.service.db") as mock_db, \
             patch("app.infra.cache.redis_client", mock_redis):
            mock_db.session = session
            result = asyncio.run(get_sys_dict_by_key("nonexistent"))
        assert result is None

    def test_get_sys_dict_by_key_model_config(self):
        from app.features.sys_dict.service import get_sys_dict_by_key
        with patch("app.features.sys_dict.service.is_model_config_sys_dict_key", return_value=True):
            result = asyncio.run(get_sys_dict_by_key("CHAT_MODEL_API"))
        assert result is None

    def test_get_sys_dict_by_key_sync(self, session):
        from app.features.sys_dict.service import get_sys_dict_by_key_sync
        from app.models.sys_dict import SysDict
        sd = SysDict(key="sync_key", value="v", des="d", enable=True)
        session.add(sd)
        session.flush()
        with patch("app.features.sys_dict.service.db") as mock_db:
            mock_db.session = session
            result = get_sys_dict_by_key_sync("sync_key")
        assert result is not None

    def test_get_sys_dict_by_key_sync_model_config(self):
        from app.features.sys_dict.service import get_sys_dict_by_key_sync
        with patch("app.features.sys_dict.service.is_model_config_sys_dict_key", return_value=True):
            result = get_sys_dict_by_key_sync("CHAT_MODEL_API")
        assert result is None

    def test_get_sys_dict(self, session):
        from app.features.sys_dict.service import get_sys_dict
        from app.models.sys_dict import SysDict
        sd = SysDict(key="get_me", value="v", des="d", enable=True)
        session.add(sd)
        session.flush()
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("app.features.sys_dict.service.db") as mock_db, \
             patch("app.infra.cache.redis_client", mock_redis):
            mock_db.session = session
            result = asyncio.run(get_sys_dict(sd.id))
        assert result["key"] == "get_me"

    def test_get_sys_dict_not_found(self, session):
        from app.features.sys_dict.service import get_sys_dict
        from app.exceptions import ResourceNotFoundError
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("app.features.sys_dict.service.db") as mock_db, \
             patch("app.infra.cache.redis_client", mock_redis):
            mock_db.session = session
            with pytest.raises(ResourceNotFoundError):
                asyncio.run(get_sys_dict(99999))


class TestDependencies:
    def test_get_current_user_no_token(self, session):
        from app.api.dependencies import get_current_user
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_current_user(None, None, session))
        assert exc_info.value.status_code == 401

    def test_get_current_user_invalid_token(self, session):
        from app.api.dependencies import get_current_user
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_current_user(None, "invalid.token", session))
        assert exc_info.value.status_code == 401

    def test_get_current_user_valid_token(self, session, test_user):
        from app.api.dependencies import get_current_user
        from app.features.auth.service import generate_token
        token = generate_token(test_user.id)
        mock_creds = MagicMock()
        mock_creds.scheme = "Bearer"
        mock_creds.credentials = token
        result = asyncio.run(get_current_user(mock_creds, None, session))
        assert result.id == test_user.id

    def test_get_current_user_query_token(self, session, test_user):
        from app.api.dependencies import get_current_user
        from app.features.auth.service import generate_token
        token = generate_token(test_user.id)
        result = asyncio.run(get_current_user(None, token, session))
        assert result.id == test_user.id

    def test_get_current_user_expired(self, session):
        from app.api.dependencies import get_current_user
        import jwt
        from app.infra.extensions import SECRET_KEY
        import datetime
        payload = {
            "exp": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1),
            "sub": "1",
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_current_user(None, token, session))
        assert exc_info.value.status_code == 401

    def test_get_current_user_mcp_revoked(self, session, test_user):
        from app.api.dependencies import get_current_user
        from app.features.auth.service import generate_mcp_token
        token = generate_mcp_token(test_user.id)
        with patch("app.mcp.token_service.get_active_mcp_token", return_value=None):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(get_current_user(None, token, session))
            assert exc_info.value.status_code == 401

    def test_get_current_user_no_sub(self, session):
        from app.api.dependencies import get_current_user
        import jwt
        from app.infra.extensions import SECRET_KEY
        token = jwt.encode({}, SECRET_KEY, algorithm="HS256")
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_current_user(None, token, session))
        assert exc_info.value.status_code == 401

    def test_get_current_user_user_not_found(self, session, test_user):
        from app.api.dependencies import get_current_user
        from app.features.auth.service import generate_token
        token = generate_token(99999)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_current_user(None, token, session))
        assert exc_info.value.status_code == 401

    def test_require_admin_admin(self, session, admin_user):
        from app.api.dependencies import require_admin
        result = require_admin(admin_user)
        assert result.role == "admin"

    def test_require_admin_not_admin(self, session, test_user):
        from app.api.dependencies import require_admin
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            require_admin(test_user)
        assert exc_info.value.status_code == 403

    def test_ensure_owner_or_admin_owner(self, session, test_user):
        from app.api.dependencies import ensure_owner_or_admin
        ensure_owner_or_admin(test_user, test_user.id)  # should not raise

    def test_ensure_owner_or_admin_admin(self, session, admin_user):
        from app.api.dependencies import ensure_owner_or_admin
        ensure_owner_or_admin(admin_user, 99999)  # admin can access anything

    def test_ensure_owner_or_admin_denied(self, session, test_user):
        from app.api.dependencies import ensure_owner_or_admin
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            ensure_owner_or_admin(test_user, 99999)
        assert exc_info.value.status_code == 403
