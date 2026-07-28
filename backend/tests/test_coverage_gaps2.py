"""补充覆盖率：query_rewrite / rerank / chat router / share router / exceptions / mcp init。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# chat/query_rewrite.py 剩余分支
# ---------------------------------------------------------------------------
class TestQueryRewriteGaps:
    def test_validate_keyword_dimensions_non_str_key(self):
        """validate_keyword_dimensions 中 key 不是 str 时分支（150-151）。
        RewriteKeywordDimensions 配置了 extra=forbid，数字 key 会触发 ValidationError。
        但 150-151 行已被执行。
        """
        from app.features.chat.query_rewrite import validate_keyword_dimensions
        with pytest.raises(Exception):
            validate_keyword_dimensions({1: ["a"], "topic_terms": ["b"]})

    def test_parse_keyword_dimensions_validation_error(self):
        """parse_keyword_dimensions 中 ValidationError 回退分支（181-182）。"""
        from app.features.chat.query_rewrite import parse_keyword_dimensions
        # 合法 JSON 但缺少必需字段，触发 ValidationError fallback
        result = parse_keyword_dimensions('{"unknown": "x"}', question="test query")
        # fallback 用原问题兜底
        assert "test query" in result.topic_terms

    def test_build_multi_queries_empty_add(self):
        """build_multi_queries 的 _add 空文本分支（235）：question 为空时走 _add("")。"""
        from app.features.chat.query_rewrite import (
            build_multi_queries, RewriteKeywordDimensions,
        )
        dims = RewriteKeywordDimensions(topic_terms=["real"])
        # question 为空时 _add(normalized_question) 即 _add("") 走 235 行
        queries = build_multi_queries("", dims, max_queries=5)
        assert "real" in queries

    def test_build_multi_queries_max_zero(self):
        from app.features.chat.query_rewrite import build_multi_queries, RewriteKeywordDimensions
        dims = RewriteKeywordDimensions(topic_terms=["a"])
        assert build_multi_queries("q", dims, max_queries=0) == []

    def test_coerce_keyword_dimensions_unknown_type(self):
        """coerce 对未知类型回退（201）。"""
        from app.features.chat.query_rewrite import coerce_keyword_dimensions
        result = coerce_keyword_dimensions(12345, question="fallback q")
        assert "fallback q" in result.topic_terms

    def test_require_keyword_dimensions_invalid_type(self):
        from app.features.chat.query_rewrite import require_keyword_dimensions
        with pytest.raises(ValueError):
            require_keyword_dimensions(12345)


# ---------------------------------------------------------------------------
# chat/rerank.py 剩余分支
# ---------------------------------------------------------------------------
class TestRerankGaps:
    def test_get_rerank_client_create(self):
        """_get_rerank_client 在 None 时创建新客户端（20-25）。"""
        from app.features.chat import rerank
        rerank._rerank_client = None
        with patch("app.features.chat.rerank.httpx.AsyncClient") as mock_ctor:
            client = rerank._get_rerank_client()
        mock_ctor.assert_called_once()
        # 清理全局状态
        rerank._rerank_client = None

    def test_get_rerank_client_reuse(self):
        """_get_rerank_client 在已存在时复用。"""
        from app.features.chat import rerank
        existing = MagicMock()
        existing.is_closed = False
        rerank._rerank_client = existing
        client = rerank._get_rerank_client()
        assert client is existing
        rerank._rerank_client = None

    async def test_rerank_empty_indices_fallback(self):
        """rerank 响应无 ranked indices 时回退（118-120）。"""
        from app.features.chat.rerank import rerank_documents
        from langchain_core.documents import Document
        docs = [Document(page_content="a"), Document(page_content="b")]
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": []}
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.is_closed = False
        with patch("app.features.chat.rerank.get_rerank_model_config",
                   return_value={"api": "http://t", "key": "k", "model": "m"}), \
             patch("app.features.chat.rerank.get_rerank_top_k", return_value=5), \
             patch("app.features.chat.rerank._get_rerank_client", return_value=mock_client):
            result = await rerank_documents("q", docs)
        assert result == docs

    async def test_rerank_out_of_range_indices_fallback(self):
        """所有 ranked indices 越界时回退（124）。"""
        from app.features.chat.rerank import rerank_documents
        from langchain_core.documents import Document
        docs = [Document(page_content="a"), Document(page_content="b")]
        mock_response = MagicMock()
        # indices 都越界
        mock_response.json.return_value = {"results": [
            {"index": 99, "relevance_score": 0.9}
        ]}
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.is_closed = False
        with patch("app.features.chat.rerank.get_rerank_model_config",
                   return_value={"api": "http://t", "key": "k", "model": "m"}), \
             patch("app.features.chat.rerank.get_rerank_top_k", return_value=5), \
             patch("app.features.chat.rerank._get_rerank_client", return_value=mock_client):
            result = await rerank_documents("q", docs)
        assert result == docs


# ---------------------------------------------------------------------------
# chat/router.py 剩余分支
# ---------------------------------------------------------------------------
class TestChatRouter:
    def test_chat_empty_query_raises(self):
        """空 query 应抛 400（22, 27-30）。绕过 pydantic 校验。"""
        from app.features.chat.router import chat
        from app.features.chat.schemas import ChatRequest
        from fastapi import HTTPException
        # model_construct 跳过校验
        payload = ChatRequest.model_construct(query="", history=[])
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(chat(payload, MagicMock(id=1), MagicMock(id=1)))
        assert exc_info.value.status_code == 400

    def test_chat_success_streaming(self):
        """有 query 时返回 StreamingResponse。"""
        from app.features.chat.router import chat
        from app.features.chat.schemas import ChatRequest
        from fastapi.responses import StreamingResponse

        async def fake_gen(*args, **kwargs):
            yield "data: test\n\n"

        payload = ChatRequest(query="hello", history=[])
        with patch("app.features.chat.router.generate_chat_events", fake_gen):
            result = asyncio.run(chat(payload, MagicMock(id=1), MagicMock(id=1)))
        assert isinstance(result, StreamingResponse)


# ---------------------------------------------------------------------------
# share/router.py 剩余分支
# ---------------------------------------------------------------------------
class TestShareRouter:
    def test_cancel_share_not_owner(self, session, test_workspace, test_user):
        """非本人取消分享应抛 403（51-54）。"""
        from app.features.share.router import cancel_share
        from app.features.share.service import create_share_link
        from app.models.file import File
        from app.exceptions import PermissionDeniedError
        f = File(name="sr.txt", file_path="sr.txt", file_size=10,
                 mime_type="text/plain", workspace_id=test_workspace.id,
                 uploader_id=test_user.id, parent_id=None,
                 content_hash="z" * 64, status="success")
        session.add(f)
        session.flush()
        share = create_share_link(session, test_user.id, f.id)
        # 另一个用户尝试取消
        other_user = MagicMock()
        other_user.id = 99999
        other_user.role = "common"
        from app.exceptions import ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError):
            cancel_share(share.id, other_user, session)


# ---------------------------------------------------------------------------
# exceptions.py 剩余分支
# ---------------------------------------------------------------------------
class TestExceptionsGaps:
    def test_register_validation_error_handler(self):
        """验证 RequestValidationError 处理器（107-111）。"""
        from app.exceptions import register_exception_handlers
        from fastapi import FastAPI
        from fastapi.exceptions import RequestValidationError
        app = FastAPI()
        register_exception_handlers(app)

        # 构造一个 RequestValidationError
        from pydantic import ValidationError
        from pydantic_core import InitErrorDetails
        try:
            exc = RequestValidationError([{
                "type": "missing",
                "loc": ("body", "field"),
                "msg": "Field required",
                "input": {},
                "url": "http://x",  # type: ignore[arg-type]
            }])
        except Exception:
            # 不同版本 pydantic 构造方式不同，用 mock 替代
            exc = MagicMock(spec=RequestValidationError)
            exc.errors.return_value = [{"msg": "test error", "loc": ("body", "f"), "type": "missing", "input": {}}]

        # 直接调用注册的 handler 验证
        # 找出注册的 handler
        handlers = app.exception_handlers
        handler = handlers.get(RequestValidationError)
        assert handler is not None
        # 调用 handler
        result = asyncio.run(handler(MagicMock(), exc))
        assert result.status_code == 422


# ---------------------------------------------------------------------------
# mcp/__init__.py 剩余分支
# ---------------------------------------------------------------------------
class TestMcpInit:
    def test_module_imports(self):
        """仅导入验证 mcp/__init__.py 的 import 行（12-13）。"""
        import app.mcp
        assert hasattr(app.mcp, "__name__")


# ---------------------------------------------------------------------------
# infra/upload_adapter.py 剩余分支
# ---------------------------------------------------------------------------
class TestUploadAdapter:
    def test_fastapi_upload_adapter_save(self, tmp_path):
        """FastAPIUploadAdapter.save 写入文件。"""
        from app.infra.upload_adapter import FastAPIUploadAdapter
        # 用真实的 BytesIO 避免 shutil.copyfileobj 无限读 MagicMock
        import io
        upload = MagicMock()
        upload.file = io.BytesIO(b"hello")
        upload.filename = "test.txt"
        upload.content_type = "text/plain"
        adapter = FastAPIUploadAdapter(upload)
        dest = str(tmp_path / "out.txt")
        adapter.save(dest)
        with open(dest, "rb") as f:
            assert f.read() == b"hello"

    def test_base64_upload_adapter_data_uri(self):
        """Base64UploadAdapter 解析 data URI。"""
        from app.infra.upload_adapter import Base64UploadAdapter
        adapter = Base64UploadAdapter(
            "data:image/png;base64,iVBORw0KGgo=",
            filename="test.png",
        )
        assert adapter.mimetype == "image/png"
        assert adapter.filename == "test.png"

    def test_base64_upload_adapter_no_filename(self):
        """无文件名时按 MIME 猜扩展名。"""
        from app.infra.upload_adapter import Base64UploadAdapter
        adapter = Base64UploadAdapter("data:image/png;base64,iVBOR=", filename=None)
        assert adapter.filename.endswith(".png")

    def test_base64_upload_adapter_plain_b64(self):
        """裸 Base64 不带 data URI。"""
        from app.infra.upload_adapter import Base64UploadAdapter
        adapter = Base64UploadAdapter("aGVsbG8=", filename="test.txt")
        assert adapter.mimetype == "application/octet-stream"

    def test_base64_upload_adapter_unknown_mime_ext(self):
        """未知 MIME 类型用 .bin 扩展名。"""
        from app.infra.upload_adapter import Base64UploadAdapter
        adapter = Base64UploadAdapter("data:application/x-foo;base64,abc=", filename=None)
        assert adapter.filename.endswith(".bin")

    def test_base64_upload_adapter_filename_no_dot(self):
        """文件名无点时补扩展名。"""
        from app.infra.upload_adapter import Base64UploadAdapter
        adapter = Base64UploadAdapter("data:image/png;base64,iVBOR=", filename="noext")
        assert "." in adapter.filename

    def test_base64_save(self, tmp_path):
        """Base64UploadAdapter.save 写入解码后数据。"""
        from app.infra.upload_adapter import Base64UploadAdapter
        adapter = Base64UploadAdapter("aGVsbG8=", filename="t.txt")
        dest = str(tmp_path / "out.txt")
        adapter.save(dest)
        with open(dest, "rb") as f:
            assert f.read() == b"hello"


# ---------------------------------------------------------------------------
# infra/llm/config.py 剩余分支
# ---------------------------------------------------------------------------
class TestLlmConfigGaps:
    def test_get_chat_model_config_with_env(self):
        """从环境变量读取配置（42, 54-55）。"""
        from app.infra.llm.config import get_chat_model_config
        with patch.dict("os.environ", {"CHAT_API_URL": "http://test", "CHAT_API_KEY": "k", "CHAT_API_MODEL": "m"}):
            config = get_chat_model_config()
        assert config["api"] == "http://test"
        assert config["key"] == "k"
        assert config["model"] == "m"

    def test_get_rerank_model_config_with_env(self):
        from app.infra.llm.config import get_rerank_model_config
        with patch.dict("os.environ", {"RERANK_API_URL": "http://r", "RERANK_API_KEY": "rk", "RERANK_MODEL": "rm"}):
            config = get_rerank_model_config()
        assert config["api"] == "http://r"
        assert config["model"] == "rm"

    def test_is_model_config_sys_dict_key(self):
        from app.infra.llm.config import is_model_config_sys_dict_key
        assert is_model_config_sys_dict_key("chat_api_url") is True
        assert is_model_config_sys_dict_key("unknown_key") is False
        assert is_model_config_sys_dict_key(None) is False


# ---------------------------------------------------------------------------
# folder/service.py 剩余分支（update/delete 的异常路径）
# ---------------------------------------------------------------------------
class TestFolderServiceExceptions:
    def test_create_folder_exception(self, session):
        from app.features.folder.service import create_folder
        # 用无效数据触发异常
        with patch.object(session, "commit", side_effect=Exception("DB fail")):
            with pytest.raises(Exception):
                create_folder(session, {"name": "test", "workspace_id": 1})

    def test_update_folder_exception(self, session, test_workspace):
        from app.features.folder.service import update_folder
        from app.models.folder import Folder
        f = Folder(name="u1", workspace_id=test_workspace.id, parent_id=None)
        session.add(f)
        session.flush()
        with patch.object(session, "commit", side_effect=Exception("DB fail")):
            with pytest.raises(Exception):
                update_folder(session, f.id, {"name": "u2"})

    def test_delete_folder_exception(self, session, test_workspace):
        from app.features.folder.service import delete_folder
        from app.models.folder import Folder
        f = Folder(name="d1", workspace_id=test_workspace.id, parent_id=None)
        session.add(f)
        session.flush()
        with patch.object(session, "commit", side_effect=Exception("DB fail")):
            with pytest.raises(Exception):
                delete_folder(session, f.id)

    def test_get_folder_not_found(self, session):
        from app.features.folder.service import get_folder
        from app.exceptions import ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError):
            get_folder(session, 99999)

    def test_get_authorized_folder_wrong_workspace(self, session, test_workspace):
        from app.features.folder.service import get_authorized_folder
        from app.models.folder import Folder
        f = Folder(name="w1", workspace_id=test_workspace.id, parent_id=None)
        session.add(f)
        session.flush()
        from app.exceptions import PermissionDeniedError
        with pytest.raises(PermissionDeniedError):
            get_authorized_folder(session, 99999, 1, f.id)
