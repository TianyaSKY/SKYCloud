"""将提取的文本切分为利于检索的文本块, 并附带来源页码."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TextSection:
    """连续的源文本区域; 为 ``None`` 时表示页码未知."""

    text: str
    page_number: int | None = None


@dataclass(frozen=True)
class TextChunk:
    """可写入向量存储的文本切片."""

    content: str
    chunk_index: int
    page_number: int | None = None


def build_text_chunks(
    sections: list[TextSection],
    chunk_size: int = 900,
    overlap: int = 150,
) -> list[TextChunk]:
    """优先在自然断句处分块, 其次按字符切分, 绝不跨页."""
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        message = "chunk_size 必须大于 overlap, 且 overlap 不能为负数"
        raise ValueError(message)

    chunks: list[TextChunk] = []
    for section in sections:
        text = "\n".join(
            line.strip() for line in section.text.splitlines() if line.strip()
        )
        if not text:
            continue

        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if end < len(text):
                boundary = max(
                    text.rfind("\n", start, end),
                    text.rfind("。", start, end),
                )
                if boundary > start + chunk_size // 2:
                    end = boundary + 1
            content = text[start:end].strip()
            if content:
                chunks.append(
                    TextChunk(content, len(chunks), section.page_number),
                )
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
    return chunks
