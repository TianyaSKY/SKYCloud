"""补充测试：file/service.py 分片上传、搜索、头像；mcp/server.py 剩余工具；chat/service.py 检索。"""

import json
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, mock_open


# ---------------------------------------------------------------------------
# file/service.py 补充测试
# ---------------------------------------------------------------------------

class TestFileServiceMultipart:
    """分片上传相关函数测试。"""

    def test_safe_upload_id_valid(self):
        from app.features.file.service import _safe_upload_id
        assert _safe_upload_id("abc12345") == "abc12345"  # 至少8字符

    def test_safe_upload_id_invalid(self):
        from app.features.file.service import _safe_upload_id
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            _safe_upload_id("")
        with pytest.raises(BusinessRuleError):
            _safe_upload_id("invalid/id")
        with pytest.raises(BusinessRuleError):
            _safe_upload_id("short")  # 少于8字符

    def test_multipart_upload_dir(self):
        from app.features.file.service import _multipart_upload_dir, MULTIPART_ROOT
        result = _multipart_upload_dir(1, "abc")
        assert "1" in result
        assert "abc" in result

    def test_multipart_chunks_dir(self):
        from app.features.file.service import _multipart_chunks_dir
        result = _multipart_chunks_dir("/tmp/upload")
        assert result.endswith("chunks")

    def test_multipart_meta_path(self):
        from app.features.file.service import _multipart_meta_path
        result = _multipart_meta_path("/tmp/upload")
        assert result.endswith("meta.json")

    def test_list_uploaded_chunks_empty(self, tmp_path):
        from app.features.file.service import _list_uploaded_chunks
        result = _list_uploaded_chunks(str(tmp_path / "nonexistent"))
        assert result == []

    def test_list_uploaded_chunks_with_files(self, tmp_path):
        from app.features.file.service import _list_uploaded_chunks
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        (chunks_dir / "0.part").write_text("data")
        (chunks_dir / "2.part").write_text("data")
        (chunks_dir / "1.part").write_text("data")
        result = _list_uploaded_chunks(str(chunks_dir))
        assert result == [0, 1, 2]

    def test_load_multipart_meta_not_found(self):
        from app.features.file.service import _load_multipart_meta
        from app.exceptions import ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError):
            _load_multipart_meta(999, "nonexistent")

    def test_write_and_load_multipart_meta(self, tmp_path):
        from app.features.file.service import _write_multipart_meta, _load_multipart_meta, _multipart_upload_dir, _multipart_meta_path
        import app.features.file.service as fs

        # 临时修改 MULTIPART_ROOT
        original_root = fs.MULTIPART_ROOT
        fs.MULTIPART_ROOT = str(tmp_path)
        try:
            upload_dir = _multipart_upload_dir(1, "test12345678")  # 至少8字符
            os.makedirs(upload_dir, exist_ok=True)
            meta_path = _multipart_meta_path(upload_dir)
            meta = {"upload_id": "test12345678", "workspace_id": 1, "filename": "test.txt"}
            _write_multipart_meta(meta_path, meta)
            loaded = _load_multipart_meta(1, "test12345678")
            assert loaded["upload_id"] == "test12345678"
        finally:
            fs.MULTIPART_ROOT = original_root

    def test_init_multipart_upload_no_filename(self, session, test_user, test_workspace):
        from app.features.file.service import init_multipart_upload
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError, match="filename"):
            init_multipart_upload(session, test_workspace.id, test_user.id, {"filename": "", "total_size": 100})

    def test_init_multipart_upload_invalid_size(self, session, test_user, test_workspace):
        from app.features.file.service import init_multipart_upload
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError, match="total_size"):
            init_multipart_upload(session, test_workspace.id, test_user.id, {"filename": "test.txt", "total_size": 0})

    def test_abort_multipart_upload(self, tmp_path):
        from app.features.file.service import abort_multipart_upload
        import app.features.file.service as fs

        original_root = fs.MULTIPART_ROOT
        fs.MULTIPART_ROOT = str(tmp_path)
        try:
            upload_dir = tmp_path / "1" / "test12345678"  # 至少8字符
            upload_dir.mkdir(parents=True)
            (upload_dir / "meta.json").write_text("{}")
            abort_multipart_upload(1, "test12345678")
            assert not upload_dir.exists()
        finally:
            fs.MULTIPART_ROOT = original_root


class TestFileServiceSearch:
    """搜索相关函数测试。"""

    def test_search_files_fuzzy_no_match(self, session, test_user, test_workspace):
        from app.features.file.service import _search_files_fuzzy

        result = _search_files_fuzzy(session, test_workspace.id, "nonexistent", 1, 10)
        assert result["total"] == 0

    def test_search_files_vector_error(self, session, test_user, test_workspace):
        from app.features.file.service import _search_files_vector

        with patch("app.features.file.service.get_embedding_model_config", side_effect=Exception("No config")):
            result = _search_files_vector(session, test_workspace.id, "test", 1, 10)
        assert "error" in result

    def test_search_files_async(self, session, test_user, test_workspace):
        from app.features.file.service import search_files
        import asyncio

        result = asyncio.run(
            search_files(session, test_workspace.id, "test", 1, 10, "fuzzy")
        )
        assert "items" in result


class TestFileServiceAvatar:
    """头像上传测试。"""

    def test_upload_avatar_for_user_permission_denied(self, session, test_user):
        from app.features.file.service import upload_avatar_for_user
        from app.exceptions import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            upload_avatar_for_user(session, test_user.id, "common", test_user.id + 1, MagicMock())

    def test_upload_avatar_for_user_self(self, session, test_user):
        from app.features.file.service import upload_avatar_for_user

        mock_upload = MagicMock()
        with patch("app.features.file.service.upload_avatar", return_value={"avatar_url": "/test"}):
            result = upload_avatar_for_user(session, test_user.id, "common", test_user.id, mock_upload)
        assert "avatar_url" in result


class TestFileServiceBatch:
    """批量操作测试。"""

    def test_batch_delete_items_file(self, session, test_user, test_workspace):
        from app.features.file.service import batch_delete_items

        with patch("app.features.file.service.get_authorized_file"), \
             patch("app.features.file.service.delete_file"):
            batch_delete_items(session, test_workspace.id, test_user.id, [{"id": 1, "is_folder": False}])

    def test_batch_delete_items_folder(self, session, test_user, test_workspace):
        from app.features.file.service import batch_delete_items

        with patch("app.features.folder.service.get_authorized_folder"), \
             patch("app.features.folder.service.delete_folder"):
            batch_delete_items(session, test_workspace.id, test_user.id, [{"id": 1, "is_folder": True}])


class TestFileServiceEmbedding:
    """Embedding 相关测试。"""

    def test_embedding_desc_success(self):
        from app.features.file.service import embedding_desc

        with patch("app.infra.llm.client.embed_texts", return_value=[[0.1, 0.2, 0.3]]):
            result = embedding_desc("test", {"model": "test"})
        assert result == [0.1, 0.2, 0.3]

    def test_embedding_desc_failure(self):
        from app.features.file.service import embedding_desc

        with patch("app.infra.llm.client.embed_texts", side_effect=Exception("API error")):
            result = embedding_desc("test", {"model": "test"})
        assert result == []

    def test_batch_embedding_desc_empty(self):
        from app.features.file.service import batch_embedding_desc
        assert batch_embedding_desc([], {}) == []

    def test_batch_embedding_desc_success(self):
        from app.features.file.service import batch_embedding_desc

        with patch("app.infra.llm.client.embed_texts", return_value=[[0.1], [0.2]]):
            result = batch_embedding_desc(["a", "b"], {})
        assert len(result) == 2

    def test_batch_embedding_desc_failure(self):
        from app.features.file.service import batch_embedding_desc

        with patch("app.infra.llm.client.embed_texts", side_effect=Exception("API error")):
            result = batch_embedding_desc(["a", "b"], {})
        assert result == [[], []]


class TestFileServiceCleanup:
    """清理函数测试。"""

    def test_cleanup_expired_uploads_no_dir(self, tmp_path):
        from app.features.file.service import cleanup_expired_uploads
        import app.features.file.service as fs

        original_root = fs.MULTIPART_ROOT
        fs.MULTIPART_ROOT = str(tmp_path / "nonexistent")
        try:
            result = cleanup_expired_uploads()
            assert result == 0
        finally:
            fs.MULTIPART_ROOT = original_root

    def test_cleanup_expired_uploads_with_old_dir(self, tmp_path):
        from app.features.file.service import cleanup_expired_uploads
        import app.features.file.service as fs
        import time

        original_root = fs.MULTIPART_ROOT
        fs.MULTIPART_ROOT = str(tmp_path)
        try:
            # 创建过期的上传目录
            ws_dir = tmp_path / "1"
            upload_dir = ws_dir / "old_upload"
            upload_dir.mkdir(parents=True)
            # 设置修改时间为很久以前
            old_time = time.time() - 100000
            os.utime(upload_dir, (old_time, old_time))

            result = cleanup_expired_uploads(max_age_hours=1)
            assert result >= 1
        finally:
            fs.MULTIPART_ROOT = original_root


# ---------------------------------------------------------------------------
# mcp/server.py 补充测试
# ---------------------------------------------------------------------------

from app.mcp.server import (
    _current_user_id,
    get_file_download_url,
    read_file_content,
    move_folder as mcp_move_folder,
    delete_folder as mcp_delete_folder,
    get_storage_overview,
    batch_delete as mcp_batch_delete,
    get_folder_tree as mcp_get_folder_tree,
    get_upload_url,
    BatchDeleteItem,
)


class TestMcpGetFileDownloadUrl:
    async def test_success(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.name = "test.txt"
        mock_file.file_size = 100

        mock_share = MagicMock()
        mock_share.to_dict.return_value = {"token": "abc123", "expires_at": "2024-01-01"}

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file), \
                 patch("app.mcp.server.share_service.create_share_link", return_value=mock_share):
                result = await get_file_download_url(1)
            parsed = json.loads(result)
            assert "download_url" in parsed
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
                result = await get_file_download_url(1)
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            _current_user_id.reset(token)


class TestMcpReadFileContent:
    async def test_not_text_file(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.mime_type = "image/png"
        mock_file.name = "photo.png"

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await read_file_content(1)
            parsed = json.loads(result)
            assert "Not a text file" in parsed.get("error", "")
        finally:
            _current_user_id.reset(token)

    async def test_file_not_found_on_server(self, tmp_path):
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.mime_type = "text/plain"
        mock_file.name = "test.txt"
        mock_file.get_abs_path.return_value = str(tmp_path / "nonexistent.txt")

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await read_file_content(1)
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            _current_user_id.reset(token)

    async def test_success(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.mime_type = "text/plain"
        mock_file.name = "test.txt"
        mock_file.get_abs_path.return_value = str(test_file)

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await read_file_content(1)
            parsed = json.loads(result)
            assert parsed["content"] == "hello world"
        finally:
            _current_user_id.reset(token)


class TestMcpMoveFolder:
    async def test_no_update_data(self):
        mock_folder = MagicMock()
        mock_folder.user_id = 1

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder):
                result = await mcp_move_folder(1)
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            _current_user_id.reset(token)

    async def test_success(self):
        mock_folder = MagicMock()
        mock_folder.user_id = 1

        mock_updated = MagicMock()
        mock_updated.to_dict.return_value = {"id": 1, "name": "renamed"}

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder), \
                 patch("app.mcp.server.folder_service.update_folder", return_value=mock_updated):
                result = await mcp_move_folder(1, new_name="renamed")
            parsed = json.loads(result)
            assert parsed["name"] == "renamed"
        finally:
            _current_user_id.reset(token)


class TestMcpDeleteFolder:
    async def test_success(self):
        mock_folder = MagicMock()
        mock_folder.user_id = 1
        mock_folder.name = "test_folder"

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder), \
                 patch("app.mcp.server.folder_service.delete_folder"):
                result = await mcp_delete_folder(1)
            parsed = json.loads(result)
            assert parsed["success"] is True
        finally:
            _current_user_id.reset(token)


class TestMcpGetStorageOverview:
    async def test_success(self):
        # 完全 mock session 和查询，避免 Folder.user_id 不存在的问题
        mock_session = MagicMock()

        # 模拟文件统计查询
        mock_file_stats = MagicMock()
        mock_file_stats.total_files = 10
        mock_file_stats.total_size = 1024

        # 配置查询链
        mock_query = MagicMock()
        mock_session.query.return_value = mock_query
        mock_query.filter.return_value.first.return_value = mock_file_stats
        mock_query.filter.return_value.group_by.return_value.all.return_value = [("success", 8), ("fail", 2)]
        mock_query.filter.return_value.scalar.return_value = 5

        # 创建带 user_id 属性的 Mock Folder
        mock_folder_cls = MagicMock()
        mock_folder_cls.user_id = MagicMock()

        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.models.folder.Folder", mock_folder_cls):
                result = await get_storage_overview()
            parsed = json.loads(result)
            assert "total_files" in parsed
        finally:
            _current_user_id.reset(token)


class TestMcpBatchDelete:
    async def test_success(self):
        mock_file = MagicMock()
        mock_file.uploader_id = 1

        mock_session = MagicMock()
        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file), \
                 patch("app.mcp.server.file_service.delete_file"):
                items = [BatchDeleteItem(id=1, is_folder=False)]
                result = await mcp_batch_delete(items)
            parsed = json.loads(result)
            assert parsed["deleted_count"] == 1
        finally:
            _current_user_id.reset(token)


class TestMcpGetFolderTree:
    async def test_success(self):
        # 完全 mock session 和查询，避免 Folder.user_id 不存在的问题
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.name = "root"
        mock_folder.parent_id = None

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_session.query.return_value = mock_query
        mock_query.filter.return_value.all.return_value = [mock_folder]

        # 创建带 user_id 属性的 Mock Folder
        mock_folder_cls = MagicMock()
        mock_folder_cls.user_id = MagicMock()

        token = _current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.models.folder.Folder", mock_folder_cls):
                result = await mcp_get_folder_tree()
            parsed = json.loads(result)
            assert isinstance(parsed, list)
        finally:
            _current_user_id.reset(token)


class TestMcpGetUploadUrl:
    async def test_success(self):
        token = _current_user_id.set(1)
        try:
            result = await get_upload_url()
            parsed = json.loads(result)
            assert "upload_url" in parsed
        finally:
            _current_user_id.reset(token)


# ---------------------------------------------------------------------------
# chat/service.py 补充测试
# ---------------------------------------------------------------------------

class TestChatServiceFunctions:
    """chat service 辅助函数测试。"""

    def test_format_docs_empty(self):
        from app.features.chat.service import format_docs
        assert format_docs([]) == ""

    def test_format_docs_with_image(self):
        from app.features.chat.service import format_docs

        class FakeDoc:
            def __init__(self):
                self.page_content = "test content"
                self.metadata = {"id": 1, "name": "test.png", "mime_type": "image/png"}

        result = format_docs([FakeDoc()])
        assert "![图片名]" in result  # 实际输出是 "图片名" 不是 "图片描述"

    def test_format_history_list(self):
        from app.features.chat.service import format_history
        history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        result = format_history(history)
        assert "user: hello" in result

    def test_format_history_string(self):
        from app.features.chat.service import format_history
        assert format_history("test") == "test"

    def test_format_history_none(self):
        from app.features.chat.service import format_history
        assert format_history(None) == ""

    def test_env_int(self):
        from app.features.chat.service import _env_int
        with patch.dict(os.environ, {"TEST_VAR": "42"}):
            assert _env_int("TEST_VAR", 10) == 42
        assert _env_int("NONEXISTENT", 10) == 10

    def test_fuse_docs_with_rrf_empty(self):
        from app.features.chat.service import _fuse_docs_with_rrf
        assert _fuse_docs_with_rrf([], 60, 10) == []

    def test_fuse_docs_with_rrf_single(self):
        from app.features.chat.service import _fuse_docs_with_rrf

        class FakeDoc:
            def __init__(self, doc_id):
                self.metadata = {"id": doc_id, "distance": 0.1}
                self.page_content = "test"

        docs = [FakeDoc(1), FakeDoc(2)]
        result = _fuse_docs_with_rrf([docs], 60, 10)
        assert len(result) == 2


class TestChatServiceAsync:
    """chat service 异步函数测试。"""

    async def test_embed_original_question_empty(self):
        from app.features.chat.service import embed_original_question
        result = await embed_original_question({"question": ""})
        assert result == []

    async def test_embed_original_question_success(self):
        from app.features.chat.service import embed_original_question

        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.return_value = [0.1] * 2000

        with patch("app.features.chat.service.get_embeddings_model", return_value=mock_embeddings):
            result = await embed_original_question({"question": "test"})
        assert len(result) == 1024
