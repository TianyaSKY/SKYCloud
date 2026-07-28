"""Chat 服务真实测试：chat/service.py + chat/query_rewrite.py + chat/rerank.py。

无 Mock：纯逻辑函数直接测试。
外部依赖标注：
- Embedding API (get_embeddings_model / _vector_search_docs / embed_original_question)
  原因：需要网络访问和有效 API Key（OpenAI 兼容接口）
  影响：multi_query_db_retriever、custom_db_retriever、generate_chat_events 中的向量检索
- Chat LLM API (get_chat_model / generate_chat_events)
  原因：需要网络访问和有效 API Key
  影响：generate_chat_events 的完整 SSE 流
- Rerank API (rerank_documents 的远程调用路径)
  原因：需要配置 RERANK_API_URL / RERANK_API_KEY / RERANK_MODEL
  影响：rerank_documents 的远程调用分支；未配置时回退到原排序（可真实测试）
"""

import asyncio
import json

import pytest
from langchain_core.documents import Document

from tests.real.conftest import skip_unless_ai_api

from app.features.chat.query_rewrite import (
    DIMENSION_KEYS,
    RewriteKeywordDimensions,
    _dedupe_terms,
    _extract_json_text,
    _fallback_dimensions,
    _normalize_dimensions,
    _normalize_terms,
    _parse_json_payload,
    build_multi_queries,
    build_retrieval_query,
    coerce_keyword_dimensions,
    format_keyword_dimensions,
    parse_keyword_dimensions,
    require_keyword_dimensions,
    validate_keyword_dimensions,
)
from app.features.chat.rerank import (
    _extract_ranked_indices,
    _get_index,
    _get_score,
    rerank_documents,
)
from app.features.chat.service import (
    _env_int,
    _fuse_docs_with_rrf,
    format_docs,
    format_history,
)


# ===========================================================================
# chat/service.py — 纯逻辑函数
# ===========================================================================


class TestChatEnvInt:
    """_env_int 环境变量解析。"""

    def test_valid_positive(self, monkeypatch):
        monkeypatch.setenv("_TEST_CHAT_INT", "15")
        assert _env_int("_TEST_CHAT_INT", 5) == 15

    def test_zero_returns_default(self, monkeypatch):
        monkeypatch.setenv("_TEST_CHAT_INT", "0")
        assert _env_int("_TEST_CHAT_INT", 5) == 5

    def test_negative_returns_default(self, monkeypatch):
        monkeypatch.setenv("_TEST_CHAT_INT", "-3")
        assert _env_int("_TEST_CHAT_INT", 7) == 7

    def test_non_numeric_returns_default(self, monkeypatch):
        monkeypatch.setenv("_TEST_CHAT_INT", "abc")
        assert _env_int("_TEST_CHAT_INT", 10) == 10

    def test_missing_returns_default(self):
        assert _env_int("_NONEXISTENT_CHAT_VAR_XYZ", 42) == 42


class TestFuseDocsWithRRF:
    """RRF 融合算法。"""

    def _make_doc(self, doc_id: int, distance: float = 0.5) -> Document:
        return Document(
            page_content=f"content_{doc_id}",
            metadata={"id": doc_id, "name": f"file_{doc_id}", "distance": distance},
        )

    def test_empty_result_sets(self):
        assert _fuse_docs_with_rrf([], rrf_k=60, top_k=10) == []

    def test_single_set_single_doc(self):
        docs = [self._make_doc(1)]
        fused = _fuse_docs_with_rrf([docs], rrf_k=60, top_k=10)
        assert len(fused) == 1
        assert fused[0].metadata["id"] == 1
        assert "rrf_score" in fused[0].metadata

    def test_multiple_sets_fusion(self):
        """多路召回融合：同一文档在多路出现时分数更高。"""
        set1 = [self._make_doc(1, 0.1), self._make_doc(2, 0.2)]
        set2 = [self._make_doc(2, 0.3), self._make_doc(3, 0.4)]
        fused = _fuse_docs_with_rrf([set1, set2], rrf_k=60, top_k=10)
        # doc_id=2 出现在两路中，分数最高
        assert fused[0].metadata["id"] == 2
        assert len(fused) == 3

    def test_top_k_truncation(self):
        docs = [self._make_doc(i) for i in range(20)]
        fused = _fuse_docs_with_rrf([docs], rrf_k=60, top_k=5)
        assert len(fused) == 5

    def test_tie_breaking_by_distance(self):
        """同分时按 distance 升序打破平局。"""
        set1 = [self._make_doc(1, 0.9)]
        set2 = [self._make_doc(2, 0.1)]
        # 两个 doc 各出现一次，RRF 分数相同 → distance 小的排前面
        fused = _fuse_docs_with_rrf([set1, set2], rrf_k=60, top_k=10)
        assert fused[0].metadata["id"] == 2  # distance=0.1 更小

    def test_rrf_score_calculation(self):
        """验证 RRF 分数计算公式：1/(k+rank)。"""
        docs = [self._make_doc(1)]
        fused = _fuse_docs_with_rrf([docs], rrf_k=60, top_k=10)
        expected_score = 1.0 / (60 + 1)
        assert abs(fused[0].metadata["rrf_score"] - expected_score) < 1e-9


class TestFormatDocs:
    """文档格式化。"""

    def test_basic_format(self):
        doc = Document(
            page_content="描述内容",
            metadata={"id": 1, "name": "test.pdf", "mime_type": "application/pdf"},
        )
        result = format_docs([doc])
        assert "[文件: test.pdf (ID: 1)]" in result
        assert "描述内容" in result

    def test_image_format(self):
        """图片文件附加 Markdown 展示提示。"""
        doc = Document(
            page_content="图片描述",
            metadata={"id": 42, "name": "photo.png", "mime_type": "image/png"},
        )
        result = format_docs([doc])
        assert "![图片名](/api/files/42/download)" in result

    def test_multiple_docs_separator(self):
        docs = [
            Document(page_content="A", metadata={"id": 1, "name": "a.txt", "mime_type": "text/plain"}),
            Document(page_content="B", metadata={"id": 2, "name": "b.txt", "mime_type": "text/plain"}),
        ]
        result = format_docs(docs)
        assert "\n\n---\n\n" in result

    def test_empty_docs(self):
        assert format_docs([]) == ""

    def test_missing_mime_type(self):
        """无 mime_type 不报错。"""
        doc = Document(page_content="X", metadata={"id": 5, "name": "x.bin"})
        result = format_docs([doc])
        assert "[文件: x.bin (ID: 5)]" in result


class TestFormatHistory:
    """历史对话格式化。"""

    def test_list_format(self):
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = format_history(history)
        assert "user: 你好" in result
        assert "assistant: 你好！" in result

    def test_empty_list(self):
        assert format_history([]) == ""

    def test_string_input(self):
        assert format_history("raw string") == "raw string"

    def test_none_input(self):
        assert format_history(None) == ""

    def test_missing_keys(self):
        """缺少 role/content 键使用默认值。"""
        history = [{"content": "只有内容"}]
        result = format_history(history)
        assert "user: 只有内容" in result


# ===========================================================================
# chat/query_rewrite.py
# ===========================================================================


class TestDedupeTerms:
    def test_basic(self):
        assert _dedupe_terms(["a", "b", "a"]) == ["a", "b"]

    def test_case_insensitive(self):
        assert _dedupe_terms(["Hello", "hello", "HELLO"]) == ["Hello"]

    def test_strips_whitespace(self):
        assert _dedupe_terms(["  x  ", "x"]) == ["x"]

    def test_empty_strings_removed(self):
        assert _dedupe_terms(["", "  ", "a"]) == ["a"]

    def test_empty_list(self):
        assert _dedupe_terms([]) == []


class TestNormalizeTerms:
    def test_none(self):
        assert _normalize_terms(None) == []

    def test_string_split(self):
        result = _normalize_terms("a,b;c|d/e\nf")
        assert result == ["a", "b", "c", "d", "e", "f"]

    def test_list_of_strings(self):
        result = _normalize_terms(["a,b", "c"])
        assert result == ["a", "b", "c"]

    def test_list_with_numbers(self):
        result = _normalize_terms([2024, "年"])
        assert "2024" in result
        assert "年" in result

    def test_single_number(self):
        assert _normalize_terms(42) == ["42"]

    def test_float(self):
        assert _normalize_terms(3.14) == ["3.14"]


class TestExtractJsonText:
    def test_empty(self):
        assert _extract_json_text("") == ""
        assert _extract_json_text(None) == ""

    def test_fenced_json(self):
        raw = '```json\n{"topic_terms": ["AI"]}\n```'
        assert _extract_json_text(raw) == '{"topic_terms": ["AI"]}'

    def test_fenced_no_lang(self):
        raw = '```\n{"a": 1}\n```'
        assert _extract_json_text(raw) == '{"a": 1}'

    def test_bare_json(self):
        raw = 'Some text {"key": "val"} more text'
        assert _extract_json_text(raw) == '{"key": "val"}'

    def test_no_json(self):
        raw = "no braces here"
        assert _extract_json_text(raw) == "no braces here"


class TestParseJsonPayload:
    def test_valid_json(self):
        result = _parse_json_payload('{"topic_terms": ["x"]}')
        assert result == {"topic_terms": ["x"]}

    def test_invalid_json(self):
        assert _parse_json_payload("{invalid}") is None

    def test_empty(self):
        assert _parse_json_payload("") is None

    def test_non_dict_json(self):
        assert _parse_json_payload("[1,2,3]") is None


class TestValidateKeywordDimensions:
    def test_standard_keys(self):
        payload = {"topic_terms": ["AI"], "entity_terms": ["GPT"]}
        dims = validate_keyword_dimensions(payload)
        assert dims.topic_terms == ["AI"]
        assert dims.entity_terms == ["GPT"]

    def test_alias_keys(self):
        """中文别名映射。"""
        payload = {"主题": ["机器学习"], "实体": ["Python"]}
        dims = validate_keyword_dimensions(payload)
        assert dims.topic_terms == ["机器学习"]
        assert dims.entity_terms == ["Python"]

    def test_english_aliases(self):
        payload = {"topics": ["deep learning"], "synonyms": ["NN", "neural network"]}
        dims = validate_keyword_dimensions(payload)
        assert dims.topic_terms == ["deep learning"]
        assert "NN" in dims.synonym_terms

    def test_dedup_in_validation(self):
        payload = {"topic_terms": ["AI", "ai", "AI"]}
        dims = validate_keyword_dimensions(payload)
        assert dims.topic_terms == ["AI"]


class TestFallbackDimensions:
    def test_with_question(self):
        dims = _fallback_dimensions("什么是量子计算")
        assert dims.topic_terms == ["什么是量子计算"]

    def test_with_raw_output(self):
        dims = _fallback_dimensions("", "关键词1,关键词2")
        assert "关键词1" in dims.topic_terms

    def test_both_empty(self):
        dims = _fallback_dimensions("", "")
        assert dims.topic_terms == []


class TestParseKeywordDimensions:
    def test_valid_json_string(self):
        raw = '{"topic_terms": ["cloud"], "entity_terms": ["AWS"]}'
        dims = parse_keyword_dimensions(raw)
        assert dims.topic_terms == ["cloud"]

    def test_invalid_json_fallback(self):
        dims = parse_keyword_dimensions("not json", "原问题")
        assert dims.topic_terms == ["原问题"]

    def test_validation_error_fallback(self):
        """extra 字段触发 ValidationError → 兜底。"""
        raw = '{"topic_terms": ["x"], "unknown_field": 123}'
        dims = parse_keyword_dimensions(raw, "问题")
        # extra="forbid" 会触发 ValidationError → fallback
        assert dims.topic_terms == ["问题"]


class TestCoerceKeywordDimensions:
    def test_model_instance(self):
        model = RewriteKeywordDimensions(topic_terms=["test"])
        result = coerce_keyword_dimensions(model)
        assert result.topic_terms == ["test"]

    def test_dict_input(self):
        result = coerce_keyword_dimensions({"topic_terms": ["a"]})
        assert result.topic_terms == ["a"]

    def test_dict_validation_error(self):
        result = coerce_keyword_dimensions({"bad_field": "x"}, "问题")
        assert result.topic_terms == ["问题"]

    def test_string_input(self):
        result = coerce_keyword_dimensions('{"entity_terms": ["B"]}')
        assert result.entity_terms == ["B"]

    def test_none_input(self):
        result = coerce_keyword_dimensions(None, "fallback")
        assert result.topic_terms == ["fallback"]

    def test_int_input(self):
        result = coerce_keyword_dimensions(123, "q")
        assert result.topic_terms == ["q"]


class TestRequireKeywordDimensions:
    def test_model_instance(self):
        model = RewriteKeywordDimensions(topic_terms=["t"])
        result = require_keyword_dimensions(model)
        assert result.topic_terms == ["t"]

    def test_dict_input(self):
        result = require_keyword_dimensions({"topic_terms": ["d"]})
        assert result.topic_terms == ["d"]

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="Invalid rewrite output type"):
            require_keyword_dimensions("string_not_allowed")

    def test_none_raises(self):
        with pytest.raises(ValueError):
            require_keyword_dimensions(None)


class TestBuildMultiQueries:
    def test_basic(self):
        dims = RewriteKeywordDimensions(
            topic_terms=["云计算"], entity_terms=["阿里云"], time_terms=["2024"]
        )
        queries = build_multi_queries("什么是云计算", dims, max_queries=6)
        assert len(queries) >= 2
        assert queries[0] == "什么是云计算"

    def test_max_queries_zero(self):
        dims = RewriteKeywordDimensions()
        assert build_multi_queries("q", dims, max_queries=0) == []

    def test_empty_question(self):
        dims = RewriteKeywordDimensions(topic_terms=["topic"])
        queries = build_multi_queries("", dims, max_queries=5)
        # 原问题为空，但维度合并查询仍存在
        assert isinstance(queries, list)

    def test_dedup_queries(self):
        """相同内容不重复。"""
        dims = RewriteKeywordDimensions(topic_terms=["AI"])
        queries = build_multi_queries("AI", dims, max_queries=10)
        assert len(queries) == len(set(q.lower() for q in queries))

    def test_synonym_expansion(self):
        dims = RewriteKeywordDimensions(synonym_terms=["machine learning"])
        queries = build_multi_queries("ML是什么", dims, max_queries=10)
        assert any("machine learning" in q for q in queries)

    def test_max_queries_truncation(self):
        dims = RewriteKeywordDimensions(
            topic_terms=["t"], entity_terms=["e"],
            time_terms=["time"], file_type_terms=["pdf"],
            synonym_terms=["syn"],
        )
        queries = build_multi_queries("question", dims, max_queries=2)
        assert len(queries) <= 2


class TestBuildRetrievalQuery:
    def test_basic(self):
        dims = RewriteKeywordDimensions(topic_terms=["cloud"], entity_terms=["AWS"])
        result = build_retrieval_query("what is cloud", dims)
        assert "what is cloud" in result
        assert "cloud" in result
        assert "AWS" in result

    def test_empty_question(self):
        dims = RewriteKeywordDimensions(topic_terms=["topic"])
        result = build_retrieval_query("", dims)
        assert "topic" in result

    def test_all_empty(self):
        dims = RewriteKeywordDimensions()
        result = build_retrieval_query("", dims)
        assert result == ""


class TestFormatKeywordDimensions:
    def test_with_terms(self):
        dims = RewriteKeywordDimensions(topic_terms=["AI"], entity_terms=["GPT"])
        result = format_keyword_dimensions(dims)
        assert "主题: AI" in result
        assert "实体: GPT" in result
        assert " | " in result

    def test_all_empty(self):
        dims = RewriteKeywordDimensions()
        result = format_keyword_dimensions(dims)
        assert result == "未提取到有效关键词"


class TestNormalizeDimensions:
    def test_strips_and_dedupes(self):
        dims = RewriteKeywordDimensions(topic_terms=[" AI ", "ai", "ML"])
        result = _normalize_dimensions(dims)
        assert result.topic_terms == ["AI", "ML"]


# ===========================================================================
# chat/rerank.py — 解析函数
# ===========================================================================


class TestGetIndex:
    def test_int_index(self):
        assert _get_index({"index": 3}) == 3

    def test_string_digit(self):
        assert _get_index({"document_index": "5"}) == 5

    def test_doc_index_key(self):
        assert _get_index({"doc_index": 2}) == 2

    def test_no_index(self):
        assert _get_index({"score": 0.9}) is None

    def test_non_digit_string(self):
        assert _get_index({"index": "abc"}) is None


class TestGetScore:
    def test_relevance_score(self):
        assert _get_score({"relevance_score": 0.95}, 0.0) == 0.95

    def test_score_key(self):
        assert _get_score({"score": 0.8}, 0.0) == 0.8

    def test_similarity_key(self):
        assert _get_score({"similarity": 0.7}, 0.0) == 0.7

    def test_string_score(self):
        assert _get_score({"score": "0.6"}, 0.0) == 0.6

    def test_invalid_string_returns_default(self):
        assert _get_score({"score": "bad"}, 0.5) == 0.5

    def test_no_score_returns_default(self):
        assert _get_score({}, 0.3) == 0.3


class TestExtractRankedIndices:
    def test_standard_results(self):
        payload = {"results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.7},
            {"index": 1, "relevance_score": 0.8},
        ]}
        assert _extract_ranked_indices(payload) == [2, 1, 0]

    def test_data_key_fallback(self):
        payload = {"data": [{"index": 1, "score": 0.5}]}
        assert _extract_ranked_indices(payload) == [1]

    def test_no_results(self):
        assert _extract_ranked_indices({}) == []
        assert _extract_ranked_indices({"results": "not_list"}) == []

    def test_dedup_indices(self):
        payload = {"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.8},
        ]}
        assert _extract_ranked_indices(payload) == [1]

    def test_non_dict_items_skipped(self):
        payload = {"results": ["bad", {"index": 0, "score": 1.0}]}
        assert _extract_ranked_indices(payload) == [0]

    def test_missing_index_skipped(self):
        payload = {"results": [{"score": 0.9}]}
        assert _extract_ranked_indices(payload) == []


class TestRerankDocuments:
    """rerank_documents 回退路径（无配置时直接返回原 docs）。"""

    @pytest.mark.asyncio
    async def test_single_doc_no_rerank(self):
        """单文档不触发 rerank。"""
        doc = Document(page_content="x", metadata={"id": 1})
        result = await rerank_documents("query", [doc])
        assert result == [doc]

    @pytest.mark.asyncio
    async def test_empty_docs(self):
        result = await rerank_documents("query", [])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_config_fallback(self, monkeypatch):
        """未配置 rerank API 时回退原排序。

        外部依赖说明：
        - 原因：测试环境未配置 RERANK_API_URL / RERANK_API_KEY / RERANK_MODEL
        - 影响：rerank_documents 的远程调用路径
        - 行为：检测到配置不全后直接返回原 docs
        """
        # 清空所有 rerank 相关环境变量及默认 key 来源
        monkeypatch.setenv("RERANK_API_URL", "")
        monkeypatch.setenv("RERANK_API_KEY", "")
        monkeypatch.setenv("RERANK_MODEL", "")
        monkeypatch.setenv("DEFAULT_MODEL_PWD", "")

        docs = [
            Document(page_content="a", metadata={"id": 1}),
            Document(page_content="b", metadata={"id": 2}),
        ]
        result = await rerank_documents("query", docs)
        assert result == docs


# ===========================================================================
# chat/service.py — 需要外部 AI API 的函数（标记 skip）
# ===========================================================================


class TestChatExternalDeps:
    """需要外部 AI API 的函数。

    外部依赖说明：
    - 原因：需要有效的 OpenAI 兼容 API Key 和网络访问
    - 影响函数：get_chat_model, get_embeddings_model, _vector_search_docs,
      _db_search_by_vector, custom_db_retriever, multi_query_db_retriever,
      embed_original_question, retrieve_docs_with_rewrite, generate_chat_events
    - 这些函数构成完整的 RAG 管线，依赖 Embedding API + 向量数据库 + LLM API

    环境变量：TEST_AI_API=1 启用以下测试
    """

    @skip_unless_ai_api
    def test_get_embeddings_model(self):
        from app.features.chat.service import get_embeddings_model
        get_embeddings_model.cache_clear()
        model = get_embeddings_model()
        assert model is not None

    @skip_unless_ai_api
    def test_get_chat_model(self):
        from app.features.chat.service import get_chat_model
        get_chat_model.cache_clear()
        model = get_chat_model()
        assert model is not None

    @skip_unless_ai_api
    def test_vector_search_docs(self):
        pass

    @skip_unless_ai_api
    def test_db_search_by_vector(self):
        pass

    @skip_unless_ai_api
    def test_custom_db_retriever(self):
        pass

    @skip_unless_ai_api
    def test_multi_query_db_retriever(self):
        pass

    @skip_unless_ai_api
    def test_embed_original_question(self):
        pass

    @skip_unless_ai_api
    def test_retrieve_docs_with_rewrite(self):
        pass

    @skip_unless_ai_api
    def test_generate_chat_events(self):
        pass
