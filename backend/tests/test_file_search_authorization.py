"""语义搜索在提供商分发与属性用量计费前必须鉴权测试."""
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typing_extensions import Self

from app.exceptions import PermissionDeniedError
from app.features.file import search_service
from app.infra.llm import client as llm


@pytest.mark.parametrize('revoked', [False, True])
async def test_semantic_search_authorization_precedes_embedding_and_usage(monkeypatch: pytest.MonkeyPatch, revoked: bool) -> None:
    loop_thread = threading.get_ident()
    events: list[Any] = []
    class Session:
        def __enter__(self) -> Self:
            assert threading.get_ident() != loop_thread
            return self
        def __exit__(self, *args: Any) -> None:
            pass
    def authorize(session: Any, workspace_id: int, user_id: int) -> None:
        assert threading.get_ident() != loop_thread
        assert (workspace_id, user_id) == (11, 7)
        events.append('authorize')
        if revoked:
            raise PermissionDeniedError('revoked')
    async def embed(**kwargs: Any) -> Any:
        events.append('provider')
        return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0]*1024)],
                               usage=SimpleNamespace(total_tokens=12, prompt_tokens=12))
    def record(usage: Any) -> None:
        assert threading.get_ident() != loop_thread
        events.append(('usage', usage.user_id, usage.total_tokens))
    def retrieve(session: Any, *args: Any) -> dict[str, Any]:
        return {'items': [], 'total': 0}
    monkeypatch.setattr(search_service, 'assert_member', authorize)
    monkeypatch.setattr(search_service, 'get_embedding_model_config', lambda: {'api': 'http://provider.test', 'key': 'disposable', 'model': 'embedding'})
    def mock_get_client(*args: Any) -> SimpleNamespace:
        return SimpleNamespace(embeddings=SimpleNamespace(create=embed))
    monkeypatch.setattr(llm, '_get_client', mock_get_client)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(llm, '_safe_record', record)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(search_service, '_search_files_vector_by_embedding', retrieve)  # pyright: ignore[reportPrivateUsage]
    if revoked:
        with pytest.raises(PermissionDeniedError):
            await search_service.search_files(None, 11, search_service.SearchFilesParams(query='query', search_type='semantic'), session_factory=cast(Any, Session), user_id=7)
        assert events == ['authorize']
    else:
        assert await search_service.search_files(None, 11, search_service.SearchFilesParams(query='query', search_type='semantic'), session_factory=cast(Any, Session), user_id=7) == {'items': [], 'total': 0}
        assert events == ['authorize', 'provider', ('usage', 7, 12), 'authorize']

