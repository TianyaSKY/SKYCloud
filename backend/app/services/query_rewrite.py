import json
import re
from typing import Any

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


def _empty_dimensions() -> dict[str, list[str]]:
    return {key: [] for key in DIMENSION_KEYS}


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

    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    if fenced_match:
        return fenced_match.group(1)

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
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


def parse_keyword_dimensions(raw_output: str, question: str = "") -> dict[str, list[str]]:
    dimensions = _empty_dimensions()
    payload = _parse_json_payload(raw_output)

    if payload:
        for key, value in payload.items():
            if not isinstance(key, str):
                continue
            mapped_key = KEY_ALIASES.get(key.strip().lower()) or KEY_ALIASES.get(key.strip())
            if mapped_key not in dimensions:
                continue
            dimensions[mapped_key].extend(_normalize_terms(value))

    for key in DIMENSION_KEYS:
        dimensions[key] = _dedupe_terms(dimensions[key])

    if all(not dimensions[key] for key in DIMENSION_KEYS):
        dimensions["topic_terms"] = _normalize_terms(raw_output)

    if not dimensions["topic_terms"] and question.strip():
        dimensions["topic_terms"] = [question.strip()]

    return dimensions


def build_retrieval_query(question: str, dimensions: dict[str, list[str]]) -> str:
    terms: list[str] = []
    if question.strip():
        terms.append(question.strip())

    for key in DIMENSION_KEYS:
        terms.extend(dimensions.get(key, []))

    merged = _dedupe_terms(terms)
    return " ".join(merged).strip()


def format_keyword_dimensions(dimensions: dict[str, list[str]]) -> str:
    chunks: list[str] = []
    for key in DIMENSION_KEYS:
        terms = dimensions.get(key, [])
        if not terms:
            continue
        label = DISPLAY_LABELS.get(key, key)
        chunks.append(f"{label}: {', '.join(terms)}")

    if not chunks:
        return "未提取到有效关键词"
    return " | ".join(chunks)
