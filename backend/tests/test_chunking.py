"""Chunk 切分与来源元数据测试。"""

import pytest

from app.infra.indexing.chunking import TextSection, build_text_chunks


def test_build_text_chunks_preserves_page_and_overlap():
    chunks = build_text_chunks(
        [TextSection("甲" * 1100, page_number=3)], chunk_size=700, overlap=100
    )

    assert len(chunks) == 2
    assert chunks[0].page_number == 3
    assert chunks[0].chunk_index == 0
    assert chunks[1].chunk_index == 1
    assert chunks[0].content[-100:] == chunks[1].content[:100]


def test_build_text_chunks_rejects_invalid_window():
    with pytest.raises(ValueError):
        build_text_chunks([TextSection("内容")], chunk_size=100, overlap=100)
