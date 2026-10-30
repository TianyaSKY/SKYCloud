"""Split extractable text into retrieval-friendly chunks with source pages."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TextSection:
    """A contiguous region of source text; the page is unknown when ``None``."""

    text: str
    page_number: int | None = None


@dataclass(frozen=True)
class TextChunk:
    """A text fragment that can be written to the vector store."""

    content: str
    chunk_index: int
    page_number: int | None = None


def build_text_chunks(
    sections: list[TextSection],
    chunk_size: int = 900,
    overlap: int = 150,
) -> list[TextChunk]:
    """Split on natural breaks first and characters second, never across pages."""
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
