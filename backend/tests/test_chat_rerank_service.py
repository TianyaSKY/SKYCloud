"""Chat 模块测试：rerank 纯函数 + service 工具函数。"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# langchain_core.documents 被 mock，创建真实 Document 类供测试
class Document:
    def __init__(self, page_content="", metadata=None):
        self.page_content = page_content
        self.metadata = metadata or {}

    def __eq__(self, other):
        return self.page_content == other.page_content and self.metadata == other.metadata


# ---------------------------------------------------------------------------
# rerank 模块纯函数
# ---------------------------------------------------------------------------

from app.features.chat.rerank import (
    _get_index,
    _get_score,
    _extract_ranked_indices,
    _get_rerank_client,
    rerank_documents,
)


class TestGetIndex:
    def test_index_int(self):
        assert _get_index({"index": 3}) == 3

    def test_document_index(self):
        assert _get_index({"document_index": 5}) == 5

    def test_doc_index(self):
        assert _get_index({"doc_index": 7}) == 7

    def test_string_digit(self):
        assert _get_index({"index": "2"}) == 2

    def test_none(self):
        assert _get_index({"foo": "bar"}) is None

    def test_non_digit_string(self):
        assert _get_index({"index": "abc"}) is None


class TestGetScore:
    def test_relevance_score(self):
        assert _get_score({"relevance_score": 0.95}, 0.0) == 0.95

    def test_score_int(self):
        assert _get_score({"score": 3}, 0.0) == 3.0

    def test_similarity(self):
        assert _get_score({"similarity": 0.8}, 0.0) == 0.8

    def test_string_score(self):
        assert _get_score({"score": "0.75"}, 0.0) == 0.75

    def test_invalid_string(self):
        assert _get_score({"score": "abc"}, 1.5) == 1.5

    def test_default(self):
        assert _get_score({}, 2.0) == 2.0


class TestExtractRankedIndices:
    def test_basic_results(self):
        payload = {"results": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}]}
        assert _extract_ranked_indices(payload) == [2, 0]

    def test_data_field(self):
        payload = {"data": [{"index": 1, "score": 0.8}, {"index": 0, "score": 0.3}]}
        assert _extract_ranked_indices(payload) == [1, 0]

    def test_no_results(self):
        assert _extract_ranked_indices({}) == []
        assert _extract_ranked_indices({"results": "not_list"}) == []

    def test_dedup(self):
        payload = {"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.8},
            {"index": 0, "relevance_score": 0.7},
        ]}
        assert _extract_ranked_indices(payload) == [1, 0]

    def test_non_dict_items_skipped(self):
        payload = {"results": ["bad", {"index": 0, "score": 1.0}]}
        assert _extract_ranked_indices(payload) == [0]

    def test_no_index_skipped(self):
        payload = {"results": [{"score": 1.0}, {"index": 2, "score": 0.5}]}
        assert _extract_ranked_indices(payload) == [2]


class TestRerankDocuments:
    async def test_single_doc_passthrough(self):
        docs = [Document(page_content="a")]
        with patch("app.features.chat.rerank.Document", Document):
            result = await rerank_documents("q", docs)
        assert result == docs

    async def test_empty_docs(self):
        result = await rerank_documents("q", [])
        assert result == []

    async def test_config_incomplete(self):
        docs = [Document(page_content="a"), Document(page_content="b")]
        with patch("app.features.chat.rerank.get_rerank_model_config", return_value={"api": "", "key": "", "model": ""}), \
             patch("app.features.chat.rerank.get_rerank_top_k", return_value=5):
            result = await rerank_documents("q", docs)
        assert result == docs

    async def test_successful_rerank(self):
        docs = [Document(page_content="a"), Document(page_content="b"), Document(page_content="c")]
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}]}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.is_closed = False

        with patch("app.features.chat.rerank.get_rerank_model_config", return_value={"api": "http://test", "key": "k", "model": "m"}), \
             patch("app.features.chat.rerank.get_rerank_top_k", return_value=5), \
             patch("app.features.chat.rerank._get_rerank_client", return_value=mock_client):
            result = await rerank_documents("q", docs)
        assert len(result) == 2
        assert result[0].page_content == "c"

    async def test_rerank_exception_fallback(self):
        docs = [Document(page_content="a"), Document(page_content="b")]
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("network error")
        mock_client.is_closed = False

        with patch("app.features.chat.rerank.get_rerank_model_config", return_value={"api": "http://t", "key": "k", "model": "m"}), \
             patch("app.features.chat.rerank.get_rerank_top_k", return_value=5), \
             patch("app.features.chat.rerank._get_rerank_client", return_value=mock_client):
            result = await rerank_documents("q", docs)
        assert result == docs


# ---------------------------------------------------------------------------
# chat service 工具函数
# ---------------------------------------------------------------------------

from app.features.chat.service import (
    _env_int as chat_env_int,
    _fuse_docs_with_rrf,
    format_docs,
    format_history,
)


class TestChatEnvInt:
    def test_valid(self, monkeypatch):
        monkeypatch.setenv("TEST_CHAT_VAR", "10")
        assert chat_env_int("TEST_CHAT_VAR", 5) == 10

    def test_invalid(self, monkeypatch):
        monkeypatch.setenv("TEST_CHAT_VAR2", "abc")
        assert chat_env_int("TEST_CHAT_VAR2", 5) == 5

    def test_zero_uses_default(self, monkeypatch):
        monkeypatch.setenv("TEST_CHAT_VAR3", "0")
        assert chat_env_int("TEST_CHAT_VAR3", 7) == 7

    def test_missing(self):
        assert chat_env_int("NONEXIST_CHAT_VAR_XYZ", 3) == 3


class TestFuseDocsWithRRF:
    def test_empty(self):
        assert _fuse_docs_with_rrf([], 60, 10) == []

    def test_single_set(self):
        docs = [
            Document(page_content="a", metadata={"id": 1, "distance": 0.1}),
            Document(page_content="b", metadata={"id": 2, "distance": 0.2}),
        ]
        result = _fuse_docs_with_rrf([docs], 60, 10)
        assert len(result) == 2
        assert result[0].metadata["id"] == 1

    def test_multi_set_fusion(self):
        set1 = [
            Document(page_content="a", metadata={"id": 1, "distance": 0.1}),
            Document(page_content="b", metadata={"id": 2, "distance": 0.2}),
        ]
        set2 = [
            Document(page_content="b", metadata={"id": 2, "distance": 0.2}),
            Document(page_content="c", metadata={"id": 3, "distance": 0.3}),
        ]
        result = _fuse_docs_with_rrf([set1, set2], 60, 10)
        # doc 2 appears in both sets so should have higher score
        assert result[0].metadata["id"] == 2

    def test_top_k_limit(self):
        docs = [Document(page_content=f"d{i}", metadata={"id": i, "distance": 0.1 * i}) for i in range(10)]
        result = _fuse_docs_with_rrf([docs], 60, 3)
        assert len(result) == 3


class TestFormatDocs:
    def test_basic(self):
        docs = [Document(page_content="内容", metadata={"id": 1, "name": "test.txt", "mime_type": "text/plain"})]
        result = format_docs(docs)
        assert "test.txt" in result
        assert "内容" in result

    def test_image_doc(self):
        docs = [Document(page_content="图片", metadata={"id": 5, "name": "img.png", "mime_type": "image/png"})]
        result = format_docs(docs)
        assert "/api/files/5/download" in result

    def test_empty(self):
        assert format_docs([]) == ""


class TestFormatHistory:
    def test_list(self):
        history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        result = format_history(history)
        assert "user: hello" in result
        assert "assistant: hi" in result

    def test_string(self):
        assert format_history("raw text") == "raw text"

    def test_none(self):
        assert format_history(None) == ""

    def test_empty_list(self):
        assert format_history([]) == ""
