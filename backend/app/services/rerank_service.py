import logging
from typing import Any

import requests
from langchain_core.documents import Document

from app.services.model_config import get_rerank_model_config, get_rerank_top_k

logger = logging.getLogger(__name__)


def _get_index(item: dict[str, Any]) -> int | None:
    for key in ("index", "document_index", "doc_index"):
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _get_score(item: dict[str, Any], default: float) -> float:
    for key in ("relevance_score", "score", "similarity"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return default


def _extract_ranked_indices(payload: dict[str, Any]) -> list[int]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = payload.get("data")
    if not isinstance(raw_results, list):
        return []

    scored: list[tuple[int, float, int]] = []
    for pos, item in enumerate(raw_results):
        if not isinstance(item, dict):
            continue
        index = _get_index(item)
        if index is None:
            continue
        score = _get_score(item, default=float(len(raw_results) - pos))
        scored.append((index, score, pos))

    scored.sort(key=lambda x: (-x[1], x[2]))
    seen: set[int] = set()
    ranked_indices: list[int] = []
    for index, _, _ in scored:
        if index in seen:
            continue
        seen.add(index)
        ranked_indices.append(index)
    return ranked_indices


def rerank_documents(query: str, docs: list[Document]) -> list[Document]:
    if len(docs) <= 1:
        return docs

    config = get_rerank_model_config()
    api_url = config.get("api", "").rstrip("/")
    api_key = config.get("key", "")
    model = config.get("model", "")
    top_k = min(get_rerank_top_k(), len(docs))

    if not api_url or not api_key or not model:
        logger.info("Rerank is skipped because api/key/model is not fully configured")
        return docs

    payload = {
        "model": model,
        "query": query,
        "documents": [doc.page_content for doc in docs],
        "top_n": top_k,
        "return_documents": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        response = requests.post(
            f"{api_url}/rerank",
            headers=headers,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        ranked_indices = _extract_ranked_indices(result)
        if not ranked_indices:
            logger.warning("Rerank response missing ranked indices, fallback to vector rank")
            return docs

        ranked_docs = [docs[i] for i in ranked_indices if 0 <= i < len(docs)]
        if not ranked_docs:
            return docs

        logger.info(f"Rerank applied: {len(docs)} -> {len(ranked_docs)}")
        return ranked_docs
    except Exception as e:
        logger.warning(f"Rerank failed, fallback to vector rank: {e}")
        return docs
