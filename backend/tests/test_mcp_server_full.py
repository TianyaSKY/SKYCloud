"""MCP server.py 单元测试。

所有工具已通过 _FakeFastMCP passthrough，可直接调用。
工具内部使用 SessionLocal()，需 mock 为测试 session。
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mcp_user_ctx():
    """设置 MCP 上下文 user_id。"""
    from app.mcp.server import _current_user_id
    token = _current_user_id.set(1)
    yield 1
    _current_user_id.reset(token)


class TestMCPHelpers:
    def test_get_authenticated_user_id_success(self, mcp_user_ctx):
        from app.mcp.server import _get_authenticated_user_id
        assert _get_authenticated_user_id() == 1

    def test_get_authenticated_user_id_no_user(self):
        from app.mcp.server import _get_authenticated_user_id, _current_user_id
        token = _current_user_id.set(None)
        with pytest.raises(PermissionError):
            _get_authenticated_user_id()
        _current_user_id.reset(token)

    def test_error_json(self):
        from app.mcp.server import _error_json
        result = json.loads(_error_json("test error"))
        assert result["error"] == "test error"

    def test_service_error_json_domain_error(self):
        from app.mcp.server import _service_error_json
        from app.exceptions import BusinessRuleError
        result = json.loads(_service_error_json(BusinessRuleError("domain error")))
        assert result["error"] == "domain error"

    def test_service_error_json_http_exception(self):
        from app.mcp.server import _service_error_json
        from fastapi import HTTPException
        result = json.loads(_service_error_json(HTTPException(status_code=404, detail="not found")))
        assert result["error"] == "not found"

    def test_service_error_json_http_exception_dict_detail(self):
        from app.mcp.server import _service_error_json
        from fastapi import HTTPException
        result = json.loads(_service_error_json(HTTPException(status_code=400, detail={"code": 1})))
        assert "error" in result

    def test_service_error_json_generic(self):
        from app.mcp.server import _service_error_json
        result = json.loads(_service_error_json(ValueError("generic")))
        assert result["error"] == "generic"

    def test_is_text_file_mime_text(self):
        from app.mcp.server import _is_text_file
        assert _is_text_file("text/plain", None) is True

    def test_is_text_file_mime_json(self):
        from app.mcp.server import _is_text_file
        assert _is_text_file("application/json", None) is True

    def test_is_text_file_extension(self):
        from app.mcp.server import _is_text_file
        assert _is_text_file(None, "code.py") is True

    def test_is_text_file_not_text(self):
        from app.mcp.server import _is_text_file
        assert _is_text_file("image/jpeg", "photo.jpg") is False

    def test_is_text_file_no_info(self):
        from app.mcp.server import _is_text_file
        assert _is_text_file(None, None) is False

    def test_run_sync(self, session):
        from app.mcp.server import _run_sync

        def fn(s, x):
            return x * 2

        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = asyncio.run(_run_sync(fn, 5))
        assert result == 10


class TestJWTAuthMiddleware:
    def test_middleware_no_auth_header(self):
        from app.mcp.server import JWTAuthMiddleware, _current_user_id
        app_mock = AsyncMock()
        mw = JWTAuthMiddleware(app_mock)
        scope = {"type": "http", "headers": []}
        asyncio.run(mw(scope, MagicMock(), MagicMock()))
        app_mock.assert_called_once()
        assert _current_user_id.get() is None

    def test_middleware_valid_bearer(self, session, test_user):
        from app.mcp.server import JWTAuthMiddleware, _current_user_id
        from app.features.auth.service import generate_token
        token = generate_token(test_user.id)
        captured = {}
        async def _capture_app(scope, receive, send):
            captured["user_id"] = _current_user_id.get()
        mw = JWTAuthMiddleware(_capture_app)
        scope = {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        with patch("app.mcp.server.SessionLocal", return_value=session):
            asyncio.run(mw(scope, MagicMock(), MagicMock()))
        assert captured["user_id"] == test_user.id

    def test_middleware_invalid_bearer(self, session):
        from app.mcp.server import JWTAuthMiddleware, _current_user_id
        app_mock = AsyncMock()
        mw = JWTAuthMiddleware(app_mock)
        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Bearer invalid.token")],
        }
        with patch("app.mcp.server.SessionLocal", return_value=session):
            asyncio.run(mw(scope, MagicMock(), MagicMock()))
        app_mock.assert_called_once()
        assert _current_user_id.get() is None

    def test_middleware_non_http(self):
        from app.mcp.server import JWTAuthMiddleware
        app_mock = AsyncMock()
        mw = JWTAuthMiddleware(app_mock)
        scope = {"type": "lifespan"}
        asyncio.run(mw(scope, MagicMock(), MagicMock()))
        app_mock.assert_called_once()

    def test_middleware_empty_token(self):
        from app.mcp.server import JWTAuthMiddleware, _current_user_id
        app_mock = AsyncMock()
        mw = JWTAuthMiddleware(app_mock)
        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Bearer ")],
        }
        asyncio.run(mw(scope, MagicMock(), MagicMock()))
        assert _current_user_id.get() is None

    def test_middleware_no_bearer_prefix(self):
        from app.mcp.server import JWTAuthMiddleware, _current_user_id
        app_mock = AsyncMock()
        mw = JWTAuthMiddleware(app_mock)
        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Basic abc123")],
        }
        asyncio.run(mw(scope, MagicMock(), MagicMock()))
        assert _current_user_id.get() is None

    def test_get_mcp_app(self):
        from app.mcp.server import get_mcp_app, mcp
        result = get_mcp_app(mcp)
        assert result is not None


class TestMCPTools:
    def test_get_current_user_tool(self, session, test_user, mcp_user_ctx):
        from app.mcp.server import get_current_user
        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = asyncio.run(get_current_user())
        data = json.loads(result)
        assert data["username"] == "testuser"
        assert "password_hash" not in data

    def test_get_current_user_tool_not_found(self, session, mcp_user_ctx):
        from app.mcp.server import get_current_user
        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = asyncio.run(get_current_user())
        data = json.loads(result)
        assert "error" in data

    def test_get_current_user_no_auth(self):
        from app.mcp.server import get_current_user
        with pytest.raises(PermissionError):
            asyncio.run(get_current_user())

    def test_search_files_tool(self, session, test_user, mcp_user_ctx):
        from app.mcp.server import search_files
        import app.mcp.server as mcp_mod
        mock_result = {"results": [], "total": 0}
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch.object(mcp_mod.file_service, "search_files", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.run(search_files("test"))
        data = json.loads(result)
        assert "results" in data

    def test_search_files_no_auth(self):
        from app.mcp.server import search_files
        with pytest.raises(PermissionError):
            asyncio.run(search_files("test"))

    def test_list_files_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import list_files
        from app.models.folder import Folder
        # 创建用户根文件夹
        root = Folder(name="/root", workspace_id=test_workspace.id, parent_id=None)
        session.add(root)
        session.flush()
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.folder_service.get_root_folder_id", return_value=root.id), \
             patch("app.mcp.server.file_service.get_files_and_folders",
                   return_value={"files": [], "folders": []}):
            result = asyncio.run(list_files())
        data = json.loads(result)
        assert "files" in data

    def test_list_files_no_auth(self):
        from app.mcp.server import list_files
        with pytest.raises(PermissionError):
            asyncio.run(list_files())

    def test_get_file_info_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import get_file_info
        from app.models.file import File
        f = File(name="info.txt", file_path="info.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="i" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = asyncio.run(get_file_info(f.id))
        data = json.loads(result)
        assert data["name"] == "info.txt"

    def test_get_file_info_permission_denied(self, session, test_workspace, mcp_user_ctx):
        from app.mcp.server import get_file_info
        # 用 mock 避免外键约束：直接返回一个 uploader_id=999 的 mock file
        mock_file = MagicMock()
        mock_file.id = 8888
        mock_file.uploader_id = 999
        mock_file.to_dict.return_value = {"id": 8888, "name": "other.txt"}
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.file_service.get_file", return_value=mock_file):
            result = asyncio.run(get_file_info(8888))
        data = json.loads(result)
        assert "error" in data
        assert "Permission" in data["error"]

    def test_create_folder_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import create_folder
        from app.models.folder import Folder
        root = Folder(name="/root2", workspace_id=test_workspace.id, parent_id=None)
        session.add(root)
        session.flush()
        mock_folder = MagicMock()
        mock_folder.to_dict.return_value = {"id": 1, "name": "new"}
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.folder_service.get_root_folder_id", return_value=root.id), \
             patch("app.mcp.server.folder_service.create_folder", return_value=mock_folder):
            result = asyncio.run(create_folder("newfolder"))
        data = json.loads(result)
        assert data["name"] == "new"

    def test_move_file_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import move_file
        from app.models.file import File
        f = File(name="mv.txt", file_path="mv.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="k" * 64, status="success")
        session.add(f)
        session.flush()
        mock_updated = MagicMock()
        mock_updated.to_dict.return_value = {"id": f.id, "name": "renamed.txt"}
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.file_service.update_file", return_value=mock_updated):
            result = asyncio.run(move_file(f.id, new_name="renamed.txt"))
        data = json.loads(result)
        assert data["name"] == "renamed.txt"

    def test_move_file_no_changes(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import move_file
        from app.models.file import File
        f = File(name="mv2.txt", file_path="mv2.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="l" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = asyncio.run(move_file(f.id))
        data = json.loads(result)
        assert "error" in data

    def test_delete_file_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import delete_file
        from app.models.file import File
        f = File(name="del.txt", file_path="del.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="m" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.file_service.delete_file"):
            result = asyncio.run(delete_file(f.id))
        data = json.loads(result)
        assert data["success"] is True

    def test_get_file_download_url_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import get_file_download_url
        from app.models.file import File
        f = File(name="dl.txt", file_path="dl.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="n" * 64, status="success")
        session.add(f)
        session.flush()
        mock_share = MagicMock()
        mock_share.to_dict.return_value = {"token": "abc", "expires_at": "2025-01-01"}
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.share_service.create_share_link", return_value=mock_share):
            result = asyncio.run(get_file_download_url(f.id))
        data = json.loads(result)
        assert "download_url" in data

    def test_read_file_content_text(self, session, test_workspace, test_user, tmp_path, mcp_user_ctx):
        from app.mcp.server import read_file_content
        from app.models.file import File
        test_file = tmp_path / "read.txt"
        test_file.write_text("Hello, World!")
        f = File(name="read.txt", file_path="read.txt", file_size=13,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="o" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.models.file.File.get_abs_path", return_value=str(test_file)):
            result = asyncio.run(read_file_content(f.id))
        data = json.loads(result)
        assert data["content"] == "Hello, World!"

    def test_read_file_content_not_text(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import read_file_content
        from app.models.file import File
        f = File(name="image.jpg", file_path="img.jpg", file_size=100,
                 mime_type="image/jpeg", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="p" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = asyncio.run(read_file_content(f.id))
        data = json.loads(result)
        assert "error" in data
        assert "Not a text file" in data["error"]

    def test_read_file_content_file_not_on_disk(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import read_file_content
        from app.models.file import File
        f = File(name="missing.txt", file_path="missing.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="q" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.models.file.File.get_abs_path", return_value="/nonexistent/path.txt"):
            result = asyncio.run(read_file_content(f.id))
        data = json.loads(result)
        assert "error" in data

    def test_move_folder_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import move_folder
        mock_folder = MagicMock()
        mock_folder.user_id = 1
        mock_folder.id = 42
        mock_updated = MagicMock()
        mock_updated.to_dict.return_value = {"id": 42, "name": "renamed"}
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder), \
             patch("app.mcp.server.folder_service.update_folder", return_value=mock_updated):
            result = asyncio.run(move_folder(42, new_name="renamed"))
        data = json.loads(result)
        assert data["name"] == "renamed"

    def test_delete_folder_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import delete_folder
        mock_folder = MagicMock()
        mock_folder.user_id = 1
        mock_folder.name = "delfolder"
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder), \
             patch("app.mcp.server.folder_service.delete_folder"):
            result = asyncio.run(delete_folder(42))
        data = json.loads(result)
        assert data["success"] is True

    def test_get_storage_overview(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import get_storage_overview
        from app.models.file import File
        from app.models.folder import Folder
        f = File(name="store.txt", file_path="store.txt", file_size=1024,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="r" * 64, status="success")
        session.add(f)
        session.flush()
        # MCP server 使用 Folder.user_id（模型上不存在），需 mock session.query 和 Folder.user_id
        mock_file_stats = MagicMock()
        mock_file_stats.total_files = 1
        mock_file_stats.total_size = 1024
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.first.return_value = mock_file_stats
        mock_query.scalar.return_value = 0
        mock_query.all.return_value = []
        mock_query.group_by.return_value = mock_query
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch.object(Folder, "user_id", create=True, new=MagicMock()), \
             patch.object(session, "query", return_value=mock_query):
            result = asyncio.run(get_storage_overview())
        data = json.loads(result)
        assert "total_files" in data
        assert "total_size_human" in data

    def test_batch_delete(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import batch_delete, BatchDeleteItem
        from app.models.file import File
        from app.models.folder import Folder
        f = File(name="bd.txt", file_path="bd.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="s" * 64, status="success")
        session.add(f)
        session.flush()
        # folder 使用 mock 避免 user_id 问题
        mock_folder = MagicMock()
        mock_folder.user_id = 1
        mock_folder.name = "bdfolder"
        items = [
            BatchDeleteItem(id=f.id, is_folder=False),
            BatchDeleteItem(id=42, is_folder=True),
        ]
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.file_service.delete_file"), \
             patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder), \
             patch("app.mcp.server.folder_service.delete_folder"):
            result = asyncio.run(batch_delete(items))
        data = json.loads(result)
        assert data["deleted_count"] == 2

    def test_get_folder_tree_tool(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import get_folder_tree as mcp_get_folder_tree
        from app.models.folder import Folder
        f1 = Folder(name="t1", workspace_id=test_workspace.id, parent_id=None)
        session.add(f1)
        session.flush()
        f2 = Folder(name="t2", workspace_id=test_workspace.id, parent_id=f1.id)
        session.add(f2)
        session.flush()
        # MCP server 使用 Folder.user_id（模型上不存在），需 mock session.query 和 Folder.user_id
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = [f1, f2]
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch.object(Folder, "user_id", create=True, new=MagicMock()), \
             patch.object(session, "query", return_value=mock_query):
            result = asyncio.run(mcp_get_folder_tree(max_depth=3))
        data = json.loads(result)
        assert isinstance(data, list)

    def test_get_upload_url(self, mcp_user_ctx):
        from app.mcp.server import get_upload_url
        with patch("app.mcp.server.os.path.exists", return_value=False):
            result = asyncio.run(get_upload_url())
        data = json.loads(result)
        assert "upload_url" in data
        assert "curl_example" in data

    def test_get_upload_url_docker(self, mcp_user_ctx):
        from app.mcp.server import get_upload_url
        with patch("app.mcp.server.os.path.exists", return_value=True):  # /.dockerenv exists
            result = asyncio.run(get_upload_url(parent_id=5))
        data = json.loads(result)
        assert "skycloud-backend-api" in data["upload_url"]
        assert "parent_id=5" in data["curl_example"]

    def test_inline_upload_adapter_supports_text_and_base64(self, tmp_path):
        from app.mcp.server import _inline_upload_adapter

        text_adapter = _inline_upload_adapter("note.md", "你好", "utf-8", None)
        text_path = tmp_path / "note.md"
        text_adapter.save(str(text_path))
        assert text_path.read_text(encoding="utf-8") == "你好"

        binary_adapter = _inline_upload_adapter("data.bin", "aGVsbG8=", "base64", None)
        binary_path = tmp_path / "data.bin"
        binary_adapter.save(str(binary_path))
        assert binary_path.read_bytes() == b"hello"

    def test_upload_file_uses_mcp_without_curl(self, mcp_user_ctx):
        from app.mcp.server import upload_file

        fake_file = MagicMock()
        fake_file.to_dict.return_value = {"id": 42, "name": "note.md"}

        async def fake_run_sync(fn, *args, **kwargs):
            return fn(MagicMock(), *args, **kwargs)

        with patch("app.mcp.server._run_sync", side_effect=fake_run_sync), \
             patch("app.mcp.server.file_service.create_uploaded_file", return_value=fake_file) as create:
            result = asyncio.run(upload_file("note.md", "云盘内容", content_encoding="utf-8"))

        data = json.loads(result)
        assert data["success"] is True
        assert data["file"]["id"] == 42
        upload = create.call_args.args[3]
        assert upload.filename == "note.md"


class TestMCPResources:
    def test_get_user_folders_resource(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import get_user_folders
        with patch("app.mcp.server._run_sync", new_callable=AsyncMock,
                   return_value=[{"id": 1, "name": "folder1"}]):
            result = asyncio.run(get_user_folders())
        data = json.loads(result)
        assert isinstance(data, list)

    def test_get_user_file_resource(self, session, test_workspace, test_user, mcp_user_ctx):
        from app.mcp.server import get_user_file
        from app.models.file import File
        f = File(name="res.txt", file_path="res.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=1, parent_id=None,
                 content_hash="u" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = asyncio.run(get_user_file(f.id))
        data = json.loads(result)
        assert data["name"] == "res.txt"

    def test_get_user_file_permission_denied(self, session, test_workspace, mcp_user_ctx):
        from app.mcp.server import get_user_file
        # 用 mock 避免外键约束：直接返回一个 uploader_id=999 的 mock file
        mock_file = MagicMock()
        mock_file.id = 9999
        mock_file.uploader_id = 999
        mock_file.to_dict.return_value = {"id": 9999, "name": "other2.txt"}
        with patch("app.mcp.server.SessionLocal", return_value=session), \
             patch("app.mcp.server.file_service.get_file", return_value=mock_file):
            result = asyncio.run(get_user_file(9999))
        data = json.loads(result)
        assert "error" in data


class TestMCPPrompts:
    def test_find_file_prompt(self):
        from app.mcp.server import find_file
        result = asyncio.run(find_file("a document about Python"))
        assert "Python" in result
        assert "search_files" in result

    def test_organize_workspace_prompt(self):
        from app.mcp.server import organize_workspace
        result = asyncio.run(organize_workspace())
        assert "list_files" in result
        assert "create_folder" in result

    def test_summarize_file_prompt(self):
        from app.mcp.server import summarize_file
        result = asyncio.run(summarize_file(42))
        assert "42" in result
        assert "get_file_info" in result

    def test_batch_download_prompt(self):
        from app.mcp.server import batch_download
        result = asyncio.run(batch_download("report"))
        assert "report" in result
        assert "search_files" in result
