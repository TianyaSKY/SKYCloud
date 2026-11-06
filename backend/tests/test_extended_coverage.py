"""补充测试：文件上传/分片用例、搜索、头像；mcp/server.py 剩余工具；chat/service.py 检索。"""
import json
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.features.file.multipart_service import MultipartInitData
from app.features.file.service import BatchDeleteItemData
from app.mcp.context import current_user_id
from app.mcp.server import (
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
from app.models.user import User
from app.models.workspace import Workspace


# file/service.py 补充测试

class TestFileServiceMultipart:
    """分片上传用例与搜索测试。"""

    def test_init_multipart_upload_no_filename(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.multipart_service import get_multipart_service
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError, match="filename"):
            get_multipart_service().init_multipart_upload(
                session, test_workspace.id, test_user.id, MultipartInitData(filename="", total_size=100),
            )

    def test_init_multipart_upload_invalid_size(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.multipart_service import get_multipart_service
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError, match="total_size"):
            get_multipart_service().init_multipart_upload(
                session, test_workspace.id, test_user.id, MultipartInitData(filename="test.txt", total_size=0),
            )

    def test_abort_multipart_upload(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        from app.features.file.multipart_service import get_multipart_service
        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            service = get_multipart_service()
            result = service.init_multipart_upload(
                session, test_workspace.id, test_user.id, MultipartInitData(filename="x", total_size=5),
            )
            upload_id = str(result["upload_id"])
            service.abort_multipart_upload(
                session, test_workspace.id, test_user.id, upload_id,
            )
        assert not (
            tmp_path / ".multipart" / str(test_workspace.id) / upload_id
        ).exists()

    def test_search_files_fuzzy_no_match(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.search_service import _search_files_fuzzy  # pyright: ignore[reportPrivateUsage]

        result = _search_files_fuzzy(session, test_workspace.id, "nonexistent", 1, 10)
        assert result["total"] == 0

    async def test_search_files_vector_error(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.search_service import SearchFilesParams, search_files

        with patch("app.features.file.search_service.get_embedding_model_config", side_effect=Exception("No config")):
            result = await search_files(session, test_workspace.id, SearchFilesParams(query="test", page=1, page_size=10, search_type="vector"))
        assert "error" in result

    def test_search_files_async(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.search_service import SearchFilesParams, search_files
        import asyncio

        result = asyncio.run(
            search_files(session, test_workspace.id, SearchFilesParams(query="test", page=1, page_size=10, search_type="fuzzy"))
        )
        assert "items" in result


class TestFileServiceAvatar:
    """头像上传测试。"""

    def test_upload_avatar_for_user_permission_denied(self, session: Session, test_user: User) -> None:
        from app.features.file.avatar_service import upload_avatar_for_user
        from app.exceptions import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            upload_avatar_for_user(session, test_user.id, "common", test_user.id + 1, MagicMock())

    def test_upload_avatar_for_user_self(
        self, session: Session, test_user: User, test_workspace: Workspace, tmp_path: Path,
    ) -> None:
        from io import BytesIO

        from fastapi import UploadFile
        from starlette.datastructures import Headers
        from app.features.file.avatar_service import upload_avatar_for_user
        from app.infra.upload_adapter import FastAPIUploadAdapter
        from app.models.share import Share
        from app.models.user import User

        mock_upload = FastAPIUploadAdapter(UploadFile(
            filename="avatar.png", file=BytesIO(b"avatar-bytes"),
            headers=Headers({"content-type": "image/png"}),
        ))
        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            result = upload_avatar_for_user(session, test_user.id, "common", test_user.id, mock_upload)
        avatar_url = cast(str, result["avatar_url"])
        assert avatar_url.startswith("/api/share/")
        assert result["name"] == "avatar.png"
        user = session.get(User, test_user.id)
        assert user is not None and user.avatar == avatar_url
        assert session.query(Share).filter_by(file_id=result["id"]).count() == 1


class TestFileServiceBatch:
    """批量操作测试。"""

    def test_batch_delete_items_file(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.service import batch_delete_items

        with patch("app.features.file.service.get_authorized_file"), \
             patch("app.features.file.service.delete_file"):
            batch_delete_items(session, test_workspace.id, test_user.id, [BatchDeleteItemData(id=1, is_folder=False)])

    def test_batch_delete_items_folder(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.service import batch_delete_items

        with patch("app.features.folder.service.get_authorized_folder"), \
             patch("app.features.folder.service.delete_folder"):
            batch_delete_items(session, test_workspace.id, test_user.id, [BatchDeleteItemData(id=1, is_folder=True)])


class TestFileServiceEmbedding:
    """Embedding 相关测试。"""

    async def test_embedding_desc_success(self) -> None:
        from app.features.file.search_service import embedding_desc

        with patch("app.features.file.search_service.embed_texts", return_value=[[0.1, 0.2, 0.3]]):
            result = await embedding_desc("test", {"model": "test"})
        assert result == [0.1, 0.2, 0.3]

    async def test_embedding_desc_failure(self) -> None:
        from app.features.file.search_service import embedding_desc

        with patch("app.features.file.search_service.embed_texts", side_effect=Exception("API error")):
            result = await embedding_desc("test", {"model": "test"})
        assert result == []

class TestFileServiceCleanup:
    """清理函数测试。"""

    def test_cleanup_expired_uploads_no_dir(self, tmp_path: Path) -> None:
        from app.features.file.service import cleanup_expired_uploads

        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path / "nonexistent")):
            result = cleanup_expired_uploads()
        assert result == 0

    def test_cleanup_expired_uploads_with_old_dir(self, tmp_path: Path) -> None:
        from app.features.file.service import cleanup_expired_uploads
        import time

        # 创建过期的分片上传目录
        ws_dir = tmp_path / ".multipart" / "1"
        upload_dir = ws_dir / "old_upload"
        upload_dir.mkdir(parents=True)
        # 设置修改时间为很久以前
        old_time = time.time() - 100000
        os.utime(upload_dir, (old_time, old_time))

        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            result = cleanup_expired_uploads(max_age_hours=1)
        assert result >= 1
        assert not upload_dir.exists()


# mcp/server.py 补充测试


class TestMcpGetFileDownloadUrl:
    async def test_success(self) -> None:
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.name = "test.txt"
        mock_file.file_size = 100

        mock_share = MagicMock()
        mock_share.to_dict.return_value = {"token": "abc123", "expires_at": "2024-01-01"}

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file), \
                 patch("app.mcp.server.share_service.create_share_link", return_value=mock_share):
                result = await get_file_download_url(1)
            parsed = cast(dict[str, Any], json.loads(result))
            assert "download_url" in parsed
        finally:
            current_user_id.reset(token)

    async def test_permission_denied(self) -> None:
        mock_file = MagicMock()
        mock_file.uploader_id = 999

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await get_file_download_url(1)
            parsed = cast(dict[str, Any], json.loads(result))
            assert "error" in parsed
        finally:
            current_user_id.reset(token)


class TestMcpReadFileContent:
    async def test_not_text_file(self) -> None:
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.mime_type = "image/png"
        mock_file.name = "photo.png"

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await read_file_content(1)
            parsed = cast(dict[str, Any], json.loads(result))
            assert "Not a text file" in cast(str, parsed.get("error", ""))
        finally:
            current_user_id.reset(token)

    async def test_file_not_found_on_server(self, tmp_path: Path) -> None:
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.mime_type = "text/plain"
        mock_file.name = "test.txt"
        mock_file.get_abs_path.return_value = str(tmp_path / "nonexistent.txt")

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await read_file_content(1)
            parsed = cast(dict[str, Any], json.loads(result))
            assert "error" in parsed
        finally:
            current_user_id.reset(token)

    async def test_success(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_file.mime_type = "text/plain"
        mock_file.name = "test.txt"
        mock_file.get_abs_path.return_value = str(test_file)

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file):
                result = await read_file_content(1)
            parsed = cast(dict[str, Any], json.loads(result))
            assert parsed["content"] == "hello world"
        finally:
            current_user_id.reset(token)


class TestMcpMoveFolder:
    async def test_no_update_data(self) -> None:
        mock_folder = MagicMock()
        mock_folder.user_id = 1

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder):
                result = await mcp_move_folder(1)
            parsed = cast(dict[str, Any], json.loads(result))
            assert "error" in parsed
        finally:
            current_user_id.reset(token)

    async def test_success(self) -> None:
        mock_folder = MagicMock()
        mock_folder.user_id = 1

        mock_updated = MagicMock()
        mock_updated.to_dict.return_value = {"id": 1, "name": "renamed"}

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder), \
                 patch("app.mcp.server.folder_service.update_folder", return_value=mock_updated):
                result = await mcp_move_folder(1, new_name="renamed")
            parsed = cast(dict[str, Any], json.loads(result))
            assert parsed["name"] == "renamed"
        finally:
            current_user_id.reset(token)


class TestMcpDeleteFolder:
    async def test_success(self) -> None:
        mock_folder = MagicMock()
        mock_folder.user_id = 1
        mock_folder.name = "test_folder"

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.folder_service.get_folder", return_value=mock_folder), \
                 patch("app.mcp.server.folder_service.delete_folder"):
                result = await mcp_delete_folder(1)
            parsed = cast(dict[str, Any], json.loads(result))
            assert parsed["success"] is True
        finally:
            current_user_id.reset(token)


class TestMcpGetStorageOverview:
    async def test_success(self) -> None:
        from app.mcp import server
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server._run_sync", new_callable=AsyncMock,
                       return_value={"total_files": 10}) as worker:
                assert json.loads(await get_storage_overview()) == {"total_files": 10}
            worker.assert_awaited_once_with(server.workspace_insights.storage_overview, 1, 1)
        finally:
            current_user_id.reset(token)


class TestMcpBatchDelete:
    async def test_success(self) -> None:
        mock_file = MagicMock()
        mock_file.uploader_id = 1

        mock_session = MagicMock()
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server.SessionLocal", return_value=mock_session), \
                 patch("app.mcp.server.file_service.get_file", return_value=mock_file), \
                 patch("app.mcp.server.file_service.delete_file"):
                items = [BatchDeleteItem(id=1, is_folder=False)]
                result = await mcp_batch_delete(items)
            parsed = cast(dict[str, Any], json.loads(result))
            assert parsed["deleted_count"] == 1
        finally:
            current_user_id.reset(token)


class TestMcpGetFolderTree:
    async def test_success(self) -> None:
        from app.mcp import server
        token = current_user_id.set(1)
        try:
            with patch("app.mcp.server._run_sync", new_callable=AsyncMock,
                       return_value=[{"id": 1, "name": "root"}]) as worker:
                assert json.loads(await mcp_get_folder_tree()) == [{"id": 1, "name": "root"}]
            worker.assert_awaited_once_with(server.workspace_insights.folder_tree, 1, 1, 3)
        finally:
            current_user_id.reset(token)


class TestMcpGetUploadUrl:
    async def test_success(self) -> None:
        token = current_user_id.set(1)
        try:
            result = await get_upload_url()
            parsed = cast(dict[str, Any], json.loads(result))
            assert "upload_url" in parsed
        finally:
            current_user_id.reset(token)


# chat/service.py 补充测试

class TestChatServiceFunctions:
    """chat service 辅助函数测试。"""

    def test_format_docs_empty(self) -> None:
        from app.features.chat.service import format_docs
        assert format_docs([]) == ""

    def test_format_docs_with_image(self) -> None:
        from app.features.chat.service import format_docs

        doc = Document(
            page_content="test content",
            metadata={"id": 1, "name": "test.png", "mime_type": "image/png"},
        )
        result = format_docs([doc])
        assert "![图片名]" in result

    def test_format_history_list(self) -> None:
        from app.features.chat.service import format_history
        history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        result = format_history(history)
        assert "user: hello" in result

    def test_format_history_string(self) -> None:
        from app.features.chat.service import format_history
        assert format_history("test") == "test"

    def test_format_history_none(self) -> None:
        from app.features.chat.service import format_history
        assert format_history(None) == ""

    def test_env_int(self) -> None:
        from app.features.chat.service import _env_int  # pyright: ignore[reportPrivateUsage]
        with patch.dict(os.environ, {"TEST_VAR": "42"}):
            assert _env_int("TEST_VAR", 10) == 42
        assert _env_int("NONEXISTENT", 10) == 10

    def test_fuse_docs_with_rrf_empty(self) -> None:
        from app.features.chat.service import _fuse_docs_with_rrf  # pyright: ignore[reportPrivateUsage]
        assert _fuse_docs_with_rrf([], 60, 10) == []

    def test_fuse_docs_with_rrf_single(self) -> None:
        from app.features.chat.service import _fuse_docs_with_rrf  # pyright: ignore[reportPrivateUsage]

        docs = [
            Document(page_content="test", metadata={"id": 1, "distance": 0.1}),
            Document(page_content="test", metadata={"id": 2, "distance": 0.1}),
        ]
        result = _fuse_docs_with_rrf([docs], 60, 10)
        assert len(result) == 2


class TestChatServiceAsync:
    """chat service 异步函数测试。"""

    async def test_embed_original_question_empty(self) -> None:
        from app.features.chat.service import embed_original_question
        result = await embed_original_question({"question": ""})
        assert result == []

    async def test_embed_original_question_success(self) -> None:
        from app.features.chat.service import embed_original_question

        mock_embeddings = MagicMock()
        mock_embeddings.aembed_query = AsyncMock(return_value=[0.1] * 2000)

        with patch("app.features.chat.service.get_embeddings_model", return_value=mock_embeddings):
            result = await embed_original_question({"question": "test"})
        assert len(result) == 1024
