from typing import Any
from unittest.mock import MagicMock

import pytest

from app.infra.indexing import handler


@pytest.mark.parametrize("vectors", [
    [[0.0] * 1024], [], [[0.0] * 1024, [0.0] * 1024], [[]], [[0.0] * 12],
    [[float("nan")] + [0.0] * 1023], [[float("inf")] + [0.0] * 1023],
])
def test_incomplete_or_invalid_vector_fails_task(monkeypatch: pytest.MonkeyPatch, vectors: list[list[float]]) -> None:
    def fake_embed(**_: Any) -> list[list[float]]:
        return vectors
    monkeypatch.setattr(handler, "embed_texts", fake_embed)
    with pytest.raises(ValueError):
        handler._embed_text("searchable text", {}, user_id=3)  # pyright: ignore[reportPrivateUsage]


def test_provider_failure_is_not_reported_as_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "embed_texts", MagicMock(side_effect=RuntimeError("provider rejected")))
    with pytest.raises(RuntimeError, match="provider rejected"):
        handler._embed_texts(["one", "two"], {}, user_id=3)  # pyright: ignore[reportPrivateUsage]


def test_valid_vectors_preserve_alignment(monkeypatch: pytest.MonkeyPatch) -> None:
    vectors = [[1.0] + [0.0] * 1023, [0.0, 1.0] + [0.0] * 1022]
    def fake_embed(**_: Any) -> list[list[float]]:
        return vectors
    monkeypatch.setattr(handler, "embed_texts", fake_embed)
    assert handler._embed_texts(["one", "two"], {}, user_id=3) == vectors  # pyright: ignore[reportPrivateUsage]
