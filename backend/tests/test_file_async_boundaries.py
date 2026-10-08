import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.features.file import router, service, search_service, avatar_upload


@pytest.mark.parametrize('search_type', ['semantic', 'vector'])
async def test_semantic_search_uses_embedding_and_vector_adapter(monkeypatch, search_type):
    embedding = AsyncMock(return_value=[0.1, 0.2])
    vector = Mock(return_value={'items': [{'id': 8}], 'total': 1})
    fuzzy = Mock(side_effect=AssertionError('semantic search used fuzzy branch'))
    monkeypatch.setattr(search_service, 'embedding_desc', embedding)
    monkeypatch.setattr(search_service, 'get_embedding_model_config', lambda: {'configured': True})
    monkeypatch.setattr(search_service, '_search_files_vector_by_embedding', vector)
    monkeypatch.setattr(search_service, '_search_files_fuzzy', fuzzy)
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
        from contextlib import nullcontext
        monkeypatch.setattr(service, 'upload_avatar_for_user', slow)
        monkeypatch.setattr(avatar_upload.account_repository, 'current_role', lambda *args: 'common')
        operation = router.upload_avatar(1, SimpleNamespace(headers={}),
            avatar=SimpleNamespace(filename='avatar.png', content_type='image/png'), avatar_base64=None,
            current_user=SimpleNamespace(id=1, role='common'), session_factory=lambda: nullcontext(Mock()))
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
    assert result == ({'success': True, 'data': {'avatar_url': '/api/share/synthetic', 'avatar': '/api/share/synthetic'}}
                      if boundary == 'avatar' else {'成功': 1})


async def test_http_semantic_parameter_reaches_vector_search(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user, get_current_workspace
    from app.infra.extensions import get_db, get_session_factory
    from contextlib import nullcontext
    embedding = AsyncMock(return_value=[0.3])
    file = dict(id=9, name='match.txt', status='success', description=None, file_size=1, mime_type=None,
                content_hash=None, workspace_id=2, uploader_id=1, parent_id=None, created_at=None)
    vector = Mock(return_value={'items': [file], 'total': 1, 'page': 1, 'page_size': 10})
    monkeypatch.setattr(search_service, 'embedding_desc', embedding)
    monkeypatch.setattr(search_service, 'get_embedding_model_config', lambda: {})
    monkeypatch.setattr(search_service, '_search_files_vector_by_embedding', vector)
    monkeypatch.setattr(search_service, '_search_files_fuzzy', Mock(side_effect=AssertionError('wrong branch')))
    session = Mock()
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: lambda: nullcontext(session)
    monkeypatch.setattr(search_service, "assert_member", Mock())
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_current_workspace] = lambda: SimpleNamespace(id=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/files/search', params={'q': 'query', 'type': 'semantic'})
    assert response.status_code == 200 and response.json()['data']['items'] == [file]
    vector.assert_called_once_with(session, 2, [0.3], 1, 10)


@pytest.mark.parametrize('mode', ['status','fuzzy','semantic'])
async def test_http_reads_own_session_and_membership_check_on_worker(monkeypatch, mode):
    import httpx
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user, get_current_workspace
    from app.infra.extensions import get_session_factory
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    sessions, checks, closed = [], [], []
    class OwnedSession:
        def __init__(self):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            sessions.append(self)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            assert threading.get_ident() == self.owner
            closed.append(self)
    def membership(db, workspace_id, user_id):
        assert threading.get_ident() == db.owner
        assert (workspace_id,user_id) == (2,1)
        checks.append(db)
    def query(db, *args):
        assert threading.get_ident() == db.owner and db in checks
        entered.set()
        assert release.wait(5)
        return {'处理中':0,'成功':1,'失败':0} if mode == 'status' else {'items':[],'total':0,'page':1,'page_size':10}
    monkeypatch.setattr(service, 'assert_member', membership)
    monkeypatch.setattr(search_service, 'assert_member', membership)
    monkeypatch.setattr(service, '_process_status', query)
    monkeypatch.setattr(search_service, '_search_files_fuzzy', query)
    monkeypatch.setattr(search_service, '_search_files_vector_by_embedding', query)
    monkeypatch.setattr(search_service, 'embedding_desc', AsyncMock(return_value=[0.1]))
    app = FastAPI(); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_current_workspace] = lambda: SimpleNamespace(id=2)
    app.dependency_overrides[get_session_factory] = lambda: OwnedSession
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        url = '/files/process_status' if mode == 'status' else '/files/search'
        pending = asyncio.create_task(client.get(url, params={'q':'target','type':mode} if mode != 'status' else {}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            async def probe():
                assert not release.is_set()
                return True
            assert await asyncio.wait_for(probe(),1)
            assert not pending.done()
        finally:
            release.set()
            result = await pending
    assert result.status_code == 200
    assert len(sessions) == 1 and checks == sessions and closed == sessions
