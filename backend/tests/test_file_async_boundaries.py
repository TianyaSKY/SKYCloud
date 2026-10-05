import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.features.file import router, service


@pytest.mark.parametrize('search_type', ['semantic', 'vector'])
async def test_semantic_search_uses_embedding_and_vector_adapter(monkeypatch, search_type):
    embedding = AsyncMock(return_value=[0.1, 0.2])
    vector = Mock(return_value={'items': [{'id': 8}], 'total': 1})
    fuzzy = Mock(side_effect=AssertionError('semantic search used fuzzy branch'))
    monkeypatch.setattr(service, 'embedding_desc', embedding)
    monkeypatch.setattr(service, 'get_embedding_model_config', lambda: {'configured': True})
    monkeypatch.setattr(service, '_search_files_vector_by_embedding', vector)
    monkeypatch.setattr(service, '_search_files_fuzzy', fuzzy)
    session = Mock()
    result = await service.search_files(session, 2, 'query', 1, 10, search_type)
    assert result['items'] == [{'id': 8}]
    embedding.assert_awaited_once_with('query', {'configured': True})
    vector.assert_called_once_with(session, 2, [0.1, 0.2], 1, 10)
    fuzzy.assert_not_called()


@pytest.mark.parametrize('boundary', ['status', 'avatar'])
async def test_slow_file_boundary_leaves_loop_responsive(monkeypatch, boundary):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        return {'avatar_url': '/api/share/synthetic'} if boundary == 'avatar' else {'成功': 1}
    if boundary == 'status':
        monkeypatch.setattr(service, '_process_status', slow)
        operation = service.process_status(Mock(), 2)
    else:
        monkeypatch.setattr(service, 'upload_avatar_for_user', slow)
        operation = router.upload_avatar(1, SimpleNamespace(headers={}),
            avatar=SimpleNamespace(filename='avatar.png', content_type='image/png'), avatar_base64=None,
            current_user=SimpleNamespace(id=1, role='common'), session=Mock())
    pending = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        async def probe():
            assert not release.is_set()
            return True
        assert await asyncio.wait_for(probe(), 1)
        assert not pending.done()
    finally:
        release.set()
        result = await pending
    assert result == ({'avatar_url': '/api/share/synthetic', 'avatar': '/api/share/synthetic'}
                      if boundary == 'avatar' else {'成功': 1})


async def test_http_semantic_parameter_reaches_vector_search(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user, get_current_workspace
    from app.infra.extensions import get_db
    embedding = AsyncMock(return_value=[0.3])
    vector = Mock(return_value={'items': [{'id': 9}], 'total': 1, 'page': 1, 'page_size': 10})
    monkeypatch.setattr(service, 'embedding_desc', embedding)
    monkeypatch.setattr(service, 'get_embedding_model_config', lambda: {})
    monkeypatch.setattr(service, '_search_files_vector_by_embedding', vector)
    monkeypatch.setattr(service, '_search_files_fuzzy', Mock(side_effect=AssertionError('wrong branch')))
    session = Mock()
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_current_workspace] = lambda: SimpleNamespace(id=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/files/search', params={'q': 'query', 'type': 'semantic'})
    assert response.status_code == 200 and response.json()['items'] == [{'id': 9}]
    vector.assert_called_once_with(session, 2, [0.3], 1, 10)
