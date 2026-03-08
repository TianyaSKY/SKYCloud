import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DIMENSION_KEYS = (
    "topic_terms",
    "entity_terms",
    "time_terms",
    "file_type_terms",
    "action_terms",
    "synonym_terms",
)

KEY_ALIASES: dict[str, str] = {
    "topic_terms": "topic_terms",
    "topics": "topic_terms",
    "topic": "topic_terms",
    "主题": "topic_terms",
    "主题词": "topic_terms",
    "entity_terms": "entity_terms",
    "entities": "entity_terms",
    "entity": "entity_terms",
    "实体": "entity_terms",
    "实体词": "entity_terms",
    "time_terms": "time_terms",
    "time": "time_terms",
    "时间": "time_terms",
    "时间词": "time_terms",
    "file_type_terms": "file_type_terms",
    "file_types": "file_type_terms",
    "file_type": "file_type_terms",
    "type_terms": "file_type_terms",
    "type": "file_type_terms",
    "文件类型": "file_type_terms",
    "动作词": "action_terms",
    "action_terms": "action_terms",
    "actions": "action_terms",
    "action": "action_terms",
    "synonym_terms": "synonym_terms",
    "synonyms": "synonym_terms",
    "同义词": "synonym_terms",
}

DISPLAY_LABELS: dict[str, str] = {
    "topic_terms": "主题",
    "entity_terms": "实体",
    "time_terms": "时间",
    "file_type_terms": "类型",
    "action_terms": "动作",
    "synonym_terms": "同义扩展",
}

TERM_SPLIT_PATTERN = re.compile(r"[,;|/\n]+")


class RewriteKeywordDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_terms: list[str] = Field(default_factory=list)
    entity_terms: list[str] = Field(default_factory=list)
    time_terms: list[str] = Field(default_factory=list)
    file_type_terms: list[str] = Field(default_factory=list)
    action_terms: list[str] = Field(default_factory=list)
    synonym_terms: list[str] = Field(default_factory=list)


def _dedupe_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        term = raw.strip()
        if not term:
            continue
        norm = term.lower()
        if norm in seen:
            continue
        seen.add(norm)
        result.append(term)
    return result


def _normalize_terms(value: Any) -> list[str]:
    if value is None:
        return []

    chunks: list[str] = []
    if isinstance(value, str):
        chunks = TERM_SPLIT_PATTERN.split(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                chunks.extend(TERM_SPLIT_PATTERN.split(item))
            elif isinstance(item, (int, float)):
                chunks.append(str(item))
    elif isinstance(value, (int, float)):
        chunks = [str(value)]

    return _dedupe_terms(chunks)


def _extract_json_text(raw_output: str) -> str:
    text = (raw_output or "").strip()
    if not text:
        return ""

    fenced_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    if fenced_match:
        return fenced_match.group(1)

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start: end + 1]
    return text


def _parse_json_payload(raw_output: str) -> dict[str, Any] | None:
    candidate = _extract_json_text(raw_output)
    if not candidate:
        return None

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_dimensions(
    dimensions: RewriteKeywordDimensions,
) -> RewriteKeywordDimensions:
    normalized: dict[str, list[str]] = {}
    for key in DIMENSION_KEYS:
        values = [item.strip() for item in getattr(dimensions, key)]
        normalized[key] = _dedupe_terms(values)
    return RewriteKeywordDimensions.model_validate(normalized)


def validate_keyword_dimensions(payload: dict[str, Any]) -> RewriteKeywordDimensions:
    normalized_payload: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            normalized_payload[key] = value
            continue
        mapped_key = KEY_ALIASES.get(
            key.strip().lower()) or KEY_ALIASES.get(key.strip())
        normalized_payload[mapped_key or key] = value

    validated = RewriteKeywordDimensions.model_validate(normalized_payload)
    return _normalize_dimensions(validated)


def _fallback_dimensions(question: str = "", raw_output: str = "") -> RewriteKeywordDimensions:
    fallback_terms: list[str] = []
    if question.strip():
        fallback_terms = [question.strip()]
    elif raw_output.strip():
        fallback_terms = _normalize_terms(raw_output)

    return RewriteKeywordDimensions(topic_terms=fallback_terms)


def parse_keyword_dimensions(
    raw_output: str, question: str = ""
) -> RewriteKeywordDimensions:
    payload = _parse_json_payload(raw_output)
    if not payload:
        return _fallback_dimensions(question, raw_output)

    try:
        return validate_keyword_dimensions(payload)
    except ValidationError:
        return _fallback_dimensions(question, raw_output)


def coerce_keyword_dimensions(
    raw_output: Any, question: str = ""
) -> RewriteKeywordDimensions:
    if isinstance(raw_output, RewriteKeywordDimensions):
        return _normalize_dimensions(raw_output)

    if isinstance(raw_output, dict):
        try:
            return validate_keyword_dimensions(raw_output)
        except ValidationError:
            return _fallback_dimensions(question)

    if isinstance(raw_output, str):
        return parse_keyword_dimensions(raw_output, question)

    return _fallback_dimensions(question)


def require_keyword_dimensions(raw_output: Any) -> RewriteKeywordDimensions:
    if isinstance(raw_output, RewriteKeywordDimensions):
        return _normalize_dimensions(raw_output)

    if isinstance(raw_output, dict):
        return validate_keyword_dimensions(raw_output)

    raise ValueError(
        "Invalid rewrite output type, expected RewriteKeywordDimensions or dict"
    )


def build_multi_queries(
    question: str,
    dimensions: RewriteKeywordDimensions,
    max_queries: int = 6,
) -> list[str]:
    """生成多样化的检索查询。

    策略:
      1. 原始问题
      2. 全维度合并查询
      3. 高价值维度组合（主题+实体、主题+时间）
      4. 问题+同义扩展
    """
    if max_queries <= 0:
        return []

    seen: set[str] = set()
    queries: list[str] = []

    def _add(query_text: str) -> None:
        text = (query_text or "").strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        queries.append(text)

    normalized_question = question.strip()

    # 1. 原始问题
    _add(normalized_question)

    # 2. 全维度合并查询
    _add(build_retrieval_query(normalized_question, dimensions))

    # 3. 高价值维度组合
    topic = " ".join(_dedupe_terms(list(dimensions.topic_terms)))
    entity = " ".join(_dedupe_terms(list(dimensions.entity_terms)))
    time_t = " ".join(_dedupe_terms(list(dimensions.time_terms)))
    file_type = " ".join(_dedupe_terms(list(dimensions.file_type_terms)))

    if topic and entity:
        _add(f"{topic} {entity}")
    if topic and time_t:
        _add(f"{topic} {time_t}")
    if topic and file_type:
        _add(f"{topic} {file_type}")
    if entity and time_t:
        _add(f"{entity} {time_t}")

    # 4. 问题 + 同义扩展词（跨语言召回）
    synonym = " ".join(_dedupe_terms(list(dimensions.synonym_terms)))
    if normalized_question and synonym:
        _add(f"{normalized_question} {synonym}")

    return queries[:max_queries]


def build_retrieval_query(question: str, dimensions: RewriteKeywordDimensions) -> str:
    terms: list[str] = []
    if question.strip():
        terms.append(question.strip())

    for key in DIMENSION_KEYS:
        terms.extend(getattr(dimensions, key))

    merged = _dedupe_terms(terms)
    return " ".join(merged).strip()


def format_keyword_dimensions(dimensions: RewriteKeywordDimensions) -> str:
    chunks: list[str] = []
    for key in DIMENSION_KEYS:
        terms = getattr(dimensions, key)
        if not terms:
            continue
        label = DISPLAY_LABELS.get(key, key)
        chunks.append(f"{label}: {', '.join(terms)}")

    if not chunks:
        return "未提取到有效关键词"
    return " | ".join(chunks)
