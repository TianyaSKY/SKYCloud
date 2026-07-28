"""MCP Server 测试：工具函数 + 中间件 + 工具处理。"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.mcp.server import (
    _get_authenticated_user_id,
    _run_sync,
    _error_json,
    _service_error_json,
    _is_text_file,
    _current_user_id,
    JWTAuthMiddleware,
    get_mcp_app,
    _TEXT_EXTENSIONS,
    _TEXT_MIME_EXACT,
    _MAX_READ_BYTES,
)
from app.exceptions import DomainError, ResourceNotFoundError
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# 工具辅助函数
# ---------------------------------------------------------------------------


class TestErrorJson:
    def test_basic(self):
        result = _error_json("something went wrong")
        parsed = json.loads(result)
        assert parsed["error"] == "something went wrong"

    def test_chinese(self):
        result = _error_json("出错了")
        assert "出错了" in result


class TestServiceErrorJson:
    def test_domain_error(self):
        exc = ResourceNotFoundError("File not found")
        result = _service_error_json(exc)
        parsed = json.loads(result)
        assert "File not found" in parsed["error"]

    def test_http_exception(self):
        exc = HTTPException(status_code=403, detail="Forbidden")
        result = _service_error_json(exc)
        parsed = json.loads(result)
        assert "Forbidden" in parsed["error"]

    def test_generic_exception(self):
        exc = ValueError("bad value")
        result = _service_error_json(exc)
        parsed = json.loads(result)
        assert "bad value" in parsed["error"]


class TestGetAuthenticatedUserId:
    def test_authenticated(self):
        token = _current_user_id.set(42)
        try:
            assert _get_authenticated_user_id() == 42
        finally:
            _current_user_id.reset(token)

    def test_not_authenticated(self):
        token = _current_user_id.set(None)
        try:
            with pytest.raises(PermissionError, match="Unauthorized"):
                _get_authenticated_user_id()
        finally:
            _current_user_id.reset(token)


class TestRunSync:
    async def test_success(self):
        def fn(session, x, y):
            return x + y

        mock_session = MagicMock()
        with patch("app.mcp.server.SessionLocal", return_value=mock_session):
            result = await _run_sync(fn, 3, 4)
        assert result == 7
        mock_session.close.assert_called_once()

    async def test_exception(self):
        def fn(session):
            raise ValueError("oops")

        mock_session = MagicMock()
        with patch("app.mcp.server.SessionLocal", return_value=mock_session):
            with pytest.raises(ValueError):
                await _run_sync(fn)
        mock_session.close.assert_called_once()


class TestIsTextFile:
    def test_text_mime(self):
        assert _is_text_file("text/plain", "file.txt") is True
        assert _is_text_file("text/html", "page.html") is True

    def test_json_mime(self):
        assert _is_text_file("application/json", "data.json") is True

    def test_extension_fallback(self):
        assert _is_text_file("application/octet-stream", "script.py") is True
        assert _is_text_file(None, "readme.md") is True

    def test_binary(self):
        assert _is_text_file("image/png", "photo.png") is False
        assert _is_text_file("application/pdf", "doc.pdf") is False

    def test_none_both(self):
        assert _is_text_file(None, None) is False


# ---------------------------------------------------------------------------
# JWT 中间件
# ---------------------------------------------------------------------------


class TestJWTAuthMiddleware:
    async def test_non_http_passthrough(self):
        mock_app = AsyncMock()
        middleware = JWTAuthMiddleware(mock_app)
        scope = {"type": "lifespan"}
        await middleware(scope, None, None)
        mock_app.assert_called_once_with(scope, None, None)

    async def test_no_auth_header(self):
        mock_app = AsyncMock()
        middleware = JWTAuthMiddleware(mock_app)
        scope = {"type": "http", "headers": []}
        await middleware(scope, None, None)
        mock_app.assert_called_once()
        # user_id should be None
        assert _current_user_id.get() is None

    async def test_valid_token(self):
        mock_app = AsyncMock()
        middleware = JWTAuthMiddleware(mock_app)
        scope = {"type": "http", "headers": [(b"authorization", b"Bearer valid_token")]}

        with patch("app.mcp.server.SessionLocal") as mock_sl, \
             patch("app.mcp.server.decode_token", return_value="42"):
            mock_session = MagicMock()
            mock_sl.return_value = mock_session
            await middleware(scope, None, None)

        mock_app.assert_called_once()
        mock_session.close.assert_called_once()

    async def test_invalid_token(self):
        mock_app = AsyncMock()
        middleware = JWTAuthMiddleware(mock_app)
        scope = {"type": "http", "headers": [(b"authorization", b"Bearer bad_token")]}

        with patch("app.mcp.server.SessionLocal") as mock_sl, \
             patch("app.mcp.server.decode_token", return_value="Invalid token"):
            mock_session = MagicMock()
            mock_sl.return_value = mock_session
            await middleware(scope, None, None)

        mock_app.assert_called_once()


class TestGetMcpApp:
    def test_wraps_with_middleware(self):
        mock_mcp = MagicMock()
        mock_mcp.streamable_http_app.return_value = MagicMock()
        result = get_mcp_app(mock_mcp)
        assert isinstance(result, JWTAuthMiddleware)


# ---------------------------------------------------------------------------
# MCP 工具函数（直接调用底层 async 函数）
# ---------------------------------------------------------------------------

from app.mcp.server import (
    get_current_user as mcp_get_current_user,
    search_files as mcp_search_files,
    list_files as mcp_list_files,
    get_file_info as mcp_get_file_info,
    create_folder as mcp_create_folder,
    move_file as mcp_move_file,
    delete_file as mcp_delete_file,
)


class TestMcpGetCurrentUser:
    async def test_success(self):
        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": 1, "username": "test", "password_hash": "secret"}

        mock_session = MagicMock()
        mock_session.get.return_value = mock_user

        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session):
                result = await mcp_get_current_user()
            parsed = json.loads(result)
            assert parsed["username"] == "test"
            assert "password_hash" not in parsed
        finally:
            _current_user_id.reset(token)

    async def test_user_not_found(self):
        mock_session = MagicMock()
        mock_session.get.return_value = None

        token = _current_user_id.set(999)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session):
                result = await mcp_get_current_user()
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            _current_user_id.reset(token)


class TestMcpSearchFiles:
    async def test_success(self):
        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.search_files", new_callable=AsyncMock, return_value={"items": [], "total": 0}):
                result = await mcp_search_files("test")
            parsed = json.loads(result)
            assert "items" in parsed
        finally:
            _current_user_id.reset(token)


class TestMcpListFiles:
    async def test_success(self):
        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.folder_service.get_root_folder_id", return_value=1), \
                 patch("app.mcp.server.file_service.get_files_and_folders", return_value={"files": [], "folders": []}):
                result = await mcp_list_files()
            parsed = json.loads(result)
            assert "files" in parsed
        finally:
            _current_user_id.reset(token)


class TestMcpGetFileInfo:
    async def test_success(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.to_dict.return_value = {"id": 1, "name": "test.txt"}

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await mcp_get_file_info(1)
            parsed = json.loads(result)
            assert parsed["name"] == "test.txt"
        finally:
            _current_user_id.reset(token)

    async def test_permission_denied(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 999  # 不同用户

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await mcp_get_file_info(1)
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            _current_user_id.reset(token)


class TestMcpCreateFolder:
    async def test_success(self):
        mock_folder = MagicMock()
        mock_folder.to_dict.return_value = {"id": 5, "name": "new_folder"}

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.folder_service.get_root_folder_id", return_value=1), \
                 patch("app.mcp.server.folder_service.create_folder", return_value=mock_folder):
                result = await mcp_create_folder("new_folder")
            parsed = json.loads(result)
            assert parsed["name"] == "new_folder"
        finally:
            _current_user_id.reset(token)


class TestMcpMoveFile:
    async def test_no_update_data(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 1

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await mcp_move_file(1)  # 无 new_name 和 new_parent_id
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            _current_user_id.reset(token)

    async def test_success(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_updated = MagicMock()
        mock_updated.to_dict.return_value = {"id": 1, "name": "renamed.txt"}

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file), \
                 patch("app.mcp.server.file_service.update_file", return_value=mock_updated):
                result = await mcp_move_file(1, new_name="renamed.txt")
            parsed = json.loads(result)
            assert parsed["name"] == "renamed.txt"
        finally:
            _current_user_id.reset(token)


class TestMcpDeleteFile:
    async def test_success(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 1

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file), \
                 patch("app.mcp.server.file_service.delete_file"):
                result = await mcp_delete_file(1)
            parsed = json.loads(result)
            assert parsed["success"] is True
        finally:
            _current_user_id.reset(token)

    async def test_permission_denied(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 999

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await mcp_delete_file(1)
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            _current_user_id.reset(token)
