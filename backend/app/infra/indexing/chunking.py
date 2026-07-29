"""将可抽取文本切成适合向量检索的、带来源页码的片段。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TextSection:
    """原始文本的一段连续区域；页码未知时为 ``None``。"""

    text: str
    page_number: int | None = None


@dataclass(frozen=True)
class TextChunk:
    """可写入向量库的文本片段。"""

    content: str
    chunk_index: int
    page_number: int | None = None


def build_text_chunks(
        sections: list[TextSection], chunk_size: int = 900, overlap: int = 150
) -> list[TextChunk]:
    """按自然段优先、按字符兜底切分，避免跨页拼接和超长片段。"""
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size 必须大于 overlap，且 overlap 不能为负数")

    chunks: list[TextChunk] = []
    for section in sections:
        text = "\n".join(line.strip() for line in section.text.splitlines() if line.strip())
        if not text:
            continue

        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if end < len(text):
                boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end))
                if boundary > start + chunk_size // 2:
                    end = boundary + 1
            content = text[start:end].strip()
            if content:
                chunks.append(TextChunk(content, len(chunks), section.page_number))
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
    return chunks
