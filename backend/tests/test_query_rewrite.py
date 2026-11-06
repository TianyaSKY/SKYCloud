"""chat query_rewrite 模块测试。"""
from typing import Any

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
    _dedupe_terms,  # pyright: ignore[reportPrivateUsage]
    _normalize_terms,  # pyright: ignore[reportPrivateUsage]
    _extract_json_text,  # pyright: ignore[reportPrivateUsage]
    _parse_json_payload,  # pyright: ignore[reportPrivateUsage]
    _fallback_dimensions,  # pyright: ignore[reportPrivateUsage]
    DIMENSION_KEYS,
)


# 内部工具函数


class TestDedupeTerms:
    def test_removes_duplicates_case_insensitive(self) -> None:
        result = _dedupe_terms(["Hello", "hello", "HELLO", "world"])  # pyright: ignore[reportPrivateUsage]
        assert result == ["Hello", "world"]

    def test_strips_whitespace(self) -> None:
        result = _dedupe_terms(["  a  ", "b", "  "])  # pyright: ignore[reportPrivateUsage]
        assert result == ["a", "b"]

    def test_empty_input(self) -> None:
        assert _dedupe_terms([]) == []  # pyright: ignore[reportPrivateUsage]


class TestNormalizeTerms:
    def test_none_returns_empty(self) -> None:
        assert _normalize_terms(None) == []  # pyright: ignore[reportPrivateUsage]

    def test_string_splits(self) -> None:
        result = _normalize_terms("a,b;c|d/e\nf")  # pyright: ignore[reportPrivateUsage]
        assert result == ["a", "b", "c", "d", "e", "f"]

    def test_list_of_strings(self) -> None:
        result = _normalize_terms(["a,b", "c"])  # pyright: ignore[reportPrivateUsage]
        assert result == ["a", "b", "c"]

    def test_numeric_values(self) -> None:
        result = _normalize_terms([2024, 3.14])  # pyright: ignore[reportPrivateUsage]
        assert result == ["2024", "3.14"]

    def test_single_number(self) -> None:
        result = _normalize_terms(42)  # pyright: ignore[reportPrivateUsage]
        assert result == ["42"]


class TestExtractJsonText:
    def test_plain_json(self) -> None:
        text = '{"topic_terms": ["ai"]}'
        assert _extract_json_text(text) == '{"topic_terms": ["ai"]}'  # pyright: ignore[reportPrivateUsage]

    def test_markdown_fence(self) -> None:
        text = '```json\n{"topic_terms": ["ai"]}\n```'
        result = _extract_json_text(text)  # pyright: ignore[reportPrivateUsage]
        assert '"topic_terms"' in result

    def test_surrounded_text(self) -> None:
        text = 'Here is the result: {"topic_terms": ["ai"]} done'
        result = _extract_json_text(text)  # pyright: ignore[reportPrivateUsage]
        assert result.startswith("{")
        assert result.endswith("}")

    def test_empty_string(self) -> None:
        assert _extract_json_text("") == ""  # pyright: ignore[reportPrivateUsage]

    def test_no_json(self) -> None:
        assert _extract_json_text("no json here") == "no json here"  # pyright: ignore[reportPrivateUsage]


class TestParseJsonPayload:
    def test_valid_json(self) -> None:
        result = _parse_json_payload('{"key": "value"}')  # pyright: ignore[reportPrivateUsage]
        assert result == {"key": "value"}

    def test_invalid_json(self) -> None:
        assert _parse_json_payload("not json") is None  # pyright: ignore[reportPrivateUsage]

    def test_non_dict_json(self) -> None:
        assert _parse_json_payload("[1,2,3]") is None  # pyright: ignore[reportPrivateUsage]

    def test_empty(self) -> None:
        assert _parse_json_payload("") is None  # pyright: ignore[reportPrivateUsage]


# validate_keyword_dimensions


class TestValidateKeywordDimensions:
    def test_standard_keys(self) -> None:
        payload: dict[str, Any] = {
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

    def test_alias_keys(self) -> None:
        payload: dict[str, Any] = {
            "topics": ["AI"],
            "entities": ["公司"],
            "时间": ["2025"],
        }
        result = validate_keyword_dimensions(payload)
        assert result.topic_terms == ["AI"]
        assert result.entity_terms == ["公司"]
        assert result.time_terms == ["2025"]

    def test_string_values_need_list_input(self) -> None:
        # validate_keyword_dimensions 期望 list 输入，字符串会触发 ValidationError
        from pydantic import ValidationError
        payload: dict[str, Any] = {"topic_terms": "AI,机器学习;深度学习"}
        with pytest.raises(ValidationError):
            validate_keyword_dimensions(payload)

    def test_list_values_dedup_and_strip(self) -> None:
        # validate_keyword_dimensions 会去重和 strip，但不拆分逗号分隔的字符串
        payload: dict[str, Any] = {"topic_terms": ["AI", "ai", " 深度学习 ", "深度学习"]}
        result = validate_keyword_dimensions(payload)
        assert "AI" in result.topic_terms
        assert "深度学习" in result.topic_terms
        # 去重后只有 2 个
        assert len(result.topic_terms) == 2

    def test_empty_payload(self) -> None:
        result = validate_keyword_dimensions({})
        for key in DIMENSION_KEYS:
            assert getattr(result, key) == []


# parse_keyword_dimensions


class TestParseKeywordDimensions:
    def test_valid_json_output(self) -> None:
        raw = '{"topic_terms": ["报告"], "entity_terms": ["公司A"]}'
        result = parse_keyword_dimensions(raw)
        assert result.topic_terms == ["报告"]

    def test_json_in_markdown(self) -> None:
        raw = '```json\n{"topic_terms": ["预算"]}\n```'
        result = parse_keyword_dimensions(raw)
        assert result.topic_terms == ["预算"]

    def test_invalid_json_fallback_to_question(self) -> None:
        raw = "这不是JSON"
        result = parse_keyword_dimensions(raw, question="原始问题")
        assert result.topic_terms == ["原始问题"]

    def test_empty_output_fallback(self) -> None:
        result = parse_keyword_dimensions("", question="测试问题")
        assert result.topic_terms == ["测试问题"]

    def test_no_question_no_raw(self) -> None:
        result = parse_keyword_dimensions("", question="")
        assert result.topic_terms == []


# coerce_keyword_dimensions


class TestCoerceKeywordDimensions:
    def test_already_dimensions(self) -> None:
        dims = RewriteKeywordDimensions(topic_terms=["x"])
        result = coerce_keyword_dimensions(dims)
        assert result.topic_terms == ["x"]

    def test_dict_input(self) -> None:
        result = coerce_keyword_dimensions({"topic_terms": ["y"]})
        assert result.topic_terms == ["y"]

    def test_string_input(self) -> None:
        result = coerce_keyword_dimensions('{"entity_terms": ["z"]}', question="q")
        assert result.entity_terms == ["z"]

    def test_invalid_type_fallback(self) -> None:
        result = coerce_keyword_dimensions(12345, question="fallback")
        assert result.topic_terms == ["fallback"]

    def test_invalid_dict_fallback(self) -> None:
        # extra keys 导致 validation error
        result = coerce_keyword_dimensions(
            {"unknown_key": ["x"]}, question="q"
        )
        # 应该兜底
        assert isinstance(result, RewriteKeywordDimensions)


# require_keyword_dimensions


class TestRequireKeywordDimensions:
    def test_dimensions_input(self) -> None:
        dims = RewriteKeywordDimensions(topic_terms=["a"])
        result = require_keyword_dimensions(dims)
        assert result.topic_terms == ["a"]

    def test_dict_input(self) -> None:
        result = require_keyword_dimensions({"topic_terms": ["b"]})
        assert result.topic_terms == ["b"]

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(ValueError):
            require_keyword_dimensions("not a dict")

    def test_invalid_dict_raises(self) -> None:
        with pytest.raises(Exception):
            require_keyword_dimensions({"invalid_extra_key": ["x"]})


# build_multi_queries


class TestBuildMultiQueries:
    def test_basic(self) -> None:
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

    def test_max_queries_limit(self) -> None:
        dims = RewriteKeywordDimensions(
            topic_terms=["a", "b"],
            entity_terms=["c"],
            time_terms=["d"],
            file_type_terms=["e"],
            synonym_terms=["f"],
        )
        queries = build_multi_queries("q", dims, max_queries=2)
        assert len(queries) <= 2

    def test_zero_max_queries(self) -> None:
        dims = RewriteKeywordDimensions(topic_terms=["x"])
        assert build_multi_queries("q", dims, max_queries=0) == []

    def test_deduplication(self) -> None:
        dims = RewriteKeywordDimensions(topic_terms=["AI"])
        queries = build_multi_queries("AI", dims)
        # "AI" 不应重复出现
        lower_queries = [q.lower() for q in queries]
        assert len(lower_queries) == len(set(lower_queries))


# build_retrieval_query


class TestBuildRetrievalQuery:
    def test_merges_all_dimensions(self) -> None:
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

    def test_empty_question(self) -> None:
        dims = RewriteKeywordDimensions(topic_terms=["AI"])
        result = build_retrieval_query("", dims)
        assert "AI" in result

    def test_empty_all(self) -> None:
        dims = RewriteKeywordDimensions()
        result = build_retrieval_query("", dims)
        assert result == ""


# format_keyword_dimensions


class TestFormatKeywordDimensions:
    def test_with_terms(self) -> None:
        dims = RewriteKeywordDimensions(
            topic_terms=["AI", "深度学习"],
            entity_terms=["公司A"],
        )
        result = format_keyword_dimensions(dims)
        assert "主题" in result
        assert "AI" in result
        assert "实体" in result
        assert "公司A" in result

    def test_empty_dimensions(self) -> None:
        dims = RewriteKeywordDimensions()
        result = format_keyword_dimensions(dims)
        assert result == "未提取到有效关键词"


# _fallback_dimensions


class TestFallbackDimensions:
    def test_uses_question(self) -> None:
        result = _fallback_dimensions(question="测试问题")  # pyright: ignore[reportPrivateUsage]
        assert result.topic_terms == ["测试问题"]

    def test_uses_raw_output_when_no_question(self) -> None:
        result = _fallback_dimensions(question="", raw_output="关键词1,关键词2")  # pyright: ignore[reportPrivateUsage]
        assert "关键词1" in result.topic_terms
        assert "关键词2" in result.topic_terms

    def test_both_empty(self) -> None:
        result = _fallback_dimensions(question="", raw_output="")  # pyright: ignore[reportPrivateUsage]
        assert result.topic_terms == []
