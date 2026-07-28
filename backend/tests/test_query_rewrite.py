"""chat query_rewrite 模块测试。"""

import pytest

from app.features.chat.query_rewrite import (
    RewriteKeywordDimensions,
    validate_keyword_dimensions,
    parse_keyword_dimensions,
    coerce_keyword_dimensions,
    require_keyword_dimensions,
    build_multi_queries,
    build_retrieval_query,
    format_keyword_dimensions,
    _dedupe_terms,
    _normalize_terms,
    _extract_json_text,
    _parse_json_payload,
    _fallback_dimensions,
    DIMENSION_KEYS,
    KEY_ALIASES,
)


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------


class TestDedupeTerms:
    def test_removes_duplicates_case_insensitive(self):
        result = _dedupe_terms(["Hello", "hello", "HELLO", "world"])
        assert result == ["Hello", "world"]

    def test_strips_whitespace(self):
        result = _dedupe_terms(["  a  ", "b", "  "])
        assert result == ["a", "b"]

    def test_empty_input(self):
        assert _dedupe_terms([]) == []


class TestNormalizeTerms:
    def test_none_returns_empty(self):
        assert _normalize_terms(None) == []

    def test_string_splits(self):
        result = _normalize_terms("a,b;c|d/e\nf")
        assert result == ["a", "b", "c", "d", "e", "f"]

    def test_list_of_strings(self):
        result = _normalize_terms(["a,b", "c"])
        assert result == ["a", "b", "c"]

    def test_numeric_values(self):
        result = _normalize_terms([2024, 3.14])
        assert result == ["2024", "3.14"]

    def test_single_number(self):
        result = _normalize_terms(42)
        assert result == ["42"]


class TestExtractJsonText:
    def test_plain_json(self):
        text = '{"topic_terms": ["ai"]}'
        assert _extract_json_text(text) == '{"topic_terms": ["ai"]}'

    def test_markdown_fence(self):
        text = '```json\n{"topic_terms": ["ai"]}\n```'
        result = _extract_json_text(text)
        assert '"topic_terms"' in result

    def test_surrounded_text(self):
        text = 'Here is the result: {"topic_terms": ["ai"]} done'
        result = _extract_json_text(text)
        assert result.startswith("{")
        assert result.endswith("}")

    def test_empty_string(self):
        assert _extract_json_text("") == ""

    def test_no_json(self):
        assert _extract_json_text("no json here") == "no json here"


class TestParseJsonPayload:
    def test_valid_json(self):
        result = _parse_json_payload('{"key": "value"}')
        assert result == {"key": "value"}

    def test_invalid_json(self):
        assert _parse_json_payload("not json") is None

    def test_non_dict_json(self):
        assert _parse_json_payload("[1,2,3]") is None

    def test_empty(self):
        assert _parse_json_payload("") is None


# ---------------------------------------------------------------------------
# validate_keyword_dimensions
# ---------------------------------------------------------------------------


class TestValidateKeywordDimensions:
    def test_standard_keys(self):
        payload = {
            "topic_terms": ["AI", "机器学习"],
            "entity_terms": ["张三"],
            "time_terms": ["2024"],
            "file_type_terms": ["pdf"],
            "action_terms": ["总结"],
            "synonym_terms": ["人工智能"],
        }
        result = validate_keyword_dimensions(payload)
        assert result.topic_terms == ["AI", "机器学习"]
        assert result.entity_terms == ["张三"]

    def test_alias_keys(self):
        payload = {
            "topics": ["AI"],
            "entities": ["公司"],
            "时间": ["2025"],
        }
        result = validate_keyword_dimensions(payload)
        assert result.topic_terms == ["AI"]
        assert result.entity_terms == ["公司"]
        assert result.time_terms == ["2025"]

    def test_string_values_need_list_input(self):
        # validate_keyword_dimensions 期望 list 输入，字符串会触发 ValidationError
        from pydantic import ValidationError
        payload = {"topic_terms": "AI,机器学习;深度学习"}
        with pytest.raises(ValidationError):
            validate_keyword_dimensions(payload)

    def test_list_values_dedup_and_strip(self):
        # validate_keyword_dimensions 会去重和 strip，但不拆分逗号分隔的字符串
        payload = {"topic_terms": ["AI", "ai", " 深度学习 ", "深度学习"]}
        result = validate_keyword_dimensions(payload)
        assert "AI" in result.topic_terms
        assert "深度学习" in result.topic_terms
        # 去重后只有 2 个
        assert len(result.topic_terms) == 2

    def test_empty_payload(self):
        result = validate_keyword_dimensions({})
        for key in DIMENSION_KEYS:
            assert getattr(result, key) == []


# ---------------------------------------------------------------------------
# parse_keyword_dimensions
# ---------------------------------------------------------------------------


class TestParseKeywordDimensions:
    def test_valid_json_output(self):
        raw = '{"topic_terms": ["报告"], "entity_terms": ["公司A"]}'
        result = parse_keyword_dimensions(raw)
        assert result.topic_terms == ["报告"]

    def test_json_in_markdown(self):
        raw = '```json\n{"topic_terms": ["预算"]}\n```'
        result = parse_keyword_dimensions(raw)
        assert result.topic_terms == ["预算"]

    def test_invalid_json_fallback_to_question(self):
        raw = "这不是JSON"
        result = parse_keyword_dimensions(raw, question="原始问题")
        assert result.topic_terms == ["原始问题"]

    def test_empty_output_fallback(self):
        result = parse_keyword_dimensions("", question="测试问题")
        assert result.topic_terms == ["测试问题"]

    def test_no_question_no_raw(self):
        result = parse_keyword_dimensions("", question="")
        assert result.topic_terms == []


# ---------------------------------------------------------------------------
# coerce_keyword_dimensions
# ---------------------------------------------------------------------------


class TestCoerceKeywordDimensions:
    def test_already_dimensions(self):
        dims = RewriteKeywordDimensions(topic_terms=["x"])
        result = coerce_keyword_dimensions(dims)
        assert result.topic_terms == ["x"]

    def test_dict_input(self):
        result = coerce_keyword_dimensions({"topic_terms": ["y"]})
        assert result.topic_terms == ["y"]

    def test_string_input(self):
        result = coerce_keyword_dimensions('{"entity_terms": ["z"]}', question="q")
        assert result.entity_terms == ["z"]

    def test_invalid_type_fallback(self):
        result = coerce_keyword_dimensions(12345, question="fallback")
        assert result.topic_terms == ["fallback"]

    def test_invalid_dict_fallback(self):
        # extra keys 导致 validation error
        result = coerce_keyword_dimensions(
            {"unknown_key": ["x"]}, question="q"
        )
        # 应该兜底
        assert isinstance(result, RewriteKeywordDimensions)


# ---------------------------------------------------------------------------
# require_keyword_dimensions
# ---------------------------------------------------------------------------


class TestRequireKeywordDimensions:
    def test_dimensions_input(self):
        dims = RewriteKeywordDimensions(topic_terms=["a"])
        result = require_keyword_dimensions(dims)
        assert result.topic_terms == ["a"]

    def test_dict_input(self):
        result = require_keyword_dimensions({"topic_terms": ["b"]})
        assert result.topic_terms == ["b"]

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError):
            require_keyword_dimensions("not a dict")

    def test_invalid_dict_raises(self):
        with pytest.raises(Exception):
            require_keyword_dimensions({"invalid_extra_key": ["x"]})


# ---------------------------------------------------------------------------
# build_multi_queries
# ---------------------------------------------------------------------------


class TestBuildMultiQueries:
    def test_basic(self):
        dims = RewriteKeywordDimensions(
            topic_terms=["AI"],
            entity_terms=["公司"],
            time_terms=["2024"],
            file_type_terms=["pdf"],
            synonym_terms=["人工智能"],
        )
        queries = build_multi_queries("查找AI报告", dims)
        assert len(queries) >= 1
        assert "查找AI报告" in queries  # 原问题始终在第一位

    def test_max_queries_limit(self):
        dims = RewriteKeywordDimensions(
            topic_terms=["a", "b"],
            entity_terms=["c"],
            time_terms=["d"],
            file_type_terms=["e"],
            synonym_terms=["f"],
        )
        queries = build_multi_queries("q", dims, max_queries=2)
        assert len(queries) <= 2

    def test_zero_max_queries(self):
        dims = RewriteKeywordDimensions(topic_terms=["x"])
        assert build_multi_queries("q", dims, max_queries=0) == []

    def test_deduplication(self):
        dims = RewriteKeywordDimensions(topic_terms=["AI"])
        queries = build_multi_queries("AI", dims)
        # "AI" 不应重复出现
        lower_queries = [q.lower() for q in queries]
        assert len(lower_queries) == len(set(lower_queries))


# ---------------------------------------------------------------------------
# build_retrieval_query
# ---------------------------------------------------------------------------


class TestBuildRetrievalQuery:
    def test_merges_all_dimensions(self):
        dims = RewriteKeywordDimensions(
            topic_terms=["AI"],
            entity_terms=["公司A"],
            time_terms=["2024"],
        )
        result = build_retrieval_query("查找报告", dims)
        assert "查找报告" in result
        assert "AI" in result
        assert "公司A" in result
        assert "2024" in result

    def test_empty_question(self):
        dims = RewriteKeywordDimensions(topic_terms=["AI"])
        result = build_retrieval_query("", dims)
        assert "AI" in result

    def test_empty_all(self):
        dims = RewriteKeywordDimensions()
        result = build_retrieval_query("", dims)
        assert result == ""


# ---------------------------------------------------------------------------
# format_keyword_dimensions
# ---------------------------------------------------------------------------


class TestFormatKeywordDimensions:
    def test_with_terms(self):
        dims = RewriteKeywordDimensions(
            topic_terms=["AI", "深度学习"],
            entity_terms=["公司A"],
        )
        result = format_keyword_dimensions(dims)
        assert "主题" in result
        assert "AI" in result
        assert "实体" in result
        assert "公司A" in result

    def test_empty_dimensions(self):
        dims = RewriteKeywordDimensions()
        result = format_keyword_dimensions(dims)
        assert result == "未提取到有效关键词"


# ---------------------------------------------------------------------------
# _fallback_dimensions
# ---------------------------------------------------------------------------


class TestFallbackDimensions:
    def test_uses_question(self):
        result = _fallback_dimensions(question="测试问题")
        assert result.topic_terms == ["测试问题"]

    def test_uses_raw_output_when_no_question(self):
        result = _fallback_dimensions(question="", raw_output="关键词1,关键词2")
        assert "关键词1" in result.topic_terms
        assert "关键词2" in result.topic_terms

    def test_both_empty(self):
        result = _fallback_dimensions(question="", raw_output="")
        assert result.topic_terms == []
