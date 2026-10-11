"""Authorization failures stop multi-query recall without private diagnostics."""
from unittest.mock import AsyncMock
import pytest
from app.exceptions import PermissionDeniedError
from app.features.chat import service, repository, retrieval_service
from app.features.chat.query_rewrite import RewriteKeywordDimensions


def dimensions():
    return RewriteKeywordDimensions(topic_terms=[],entity_terms=[],time_terms=[],
        file_type_terms=[],action_terms=[],synonym_terms=[])


@pytest.mark.parametrize('actor', [0,-1,None,True])
def test_vector_repository_rejects_missing_identity_before_session(actor):
    def forbidden(): raise AssertionError('missing actor must not open SQL')
    with pytest.raises(PermissionDeniedError): repository.vector_rows(forbidden, [], 1, actor, 10)


@pytest.mark.parametrize('permission_failure', [False,True])
async def test_multi_query_permission_abort_and_safe_partial_failure(monkeypatch, permission_failure):
    private = 'private-question-and-database-url'
    logged, actors = [], []
    monkeypatch.setattr(service, 'build_multi_queries', lambda *args,**kwargs:['ok',private])
    class Embeddings:
        aembed_documents = AsyncMock(return_value=[[1.0],[1.0]])
    monkeypatch.setattr(service, 'get_embeddings_model', lambda: Embeddings())
    def search(vector, query, workspace, limit, actor):
        actors.append(actor)
        if query == private:
            if permission_failure: raise PermissionDeniedError(private)
            raise RuntimeError(private)
        return ['authorized-result']
    rerank = AsyncMock(return_value=['reranked'])
    monkeypatch.setattr(service, '_db_search_by_vector', search)
    monkeypatch.setattr(service, '_fuse_docs_with_rrf', lambda *args,**kwargs:['authorized-result'])
    monkeypatch.setattr(service, 'rerank_documents', rerank)
    monkeypatch.setattr(retrieval_service.logger, 'warning', lambda message,*args:logged.append(message.format(*args)))
    if permission_failure:
        with pytest.raises(PermissionDeniedError):
            await service.multi_query_db_retriever(private,1,dimensions(),user_id=7)
        rerank.assert_not_called()
        assert logged == []
    else:
        assert await service.multi_query_db_retriever(private,1,dimensions(),user_id=7) == ['reranked']
        assert logged and private not in str(logged)
    assert sorted(actors) == [7,7]


@pytest.mark.parametrize('denied', [False,True])
def test_retrieval_cleanup_does_not_replace_rows_or_permission_error(monkeypatch, denied):
    from unittest.mock import Mock
    session = Mock()
    rows = [('authorized',)]
    session.execute.return_value.fetchall.return_value = rows
    session.close.side_effect = RuntimeError('private-connection-url')
    session.invalidate.side_effect = RuntimeError('private-invalidation-url')
    original = PermissionDeniedError('denied')
    def authorize(*args):
        if denied: raise original
    logged = []
    monkeypatch.setattr(repository, 'assert_member_locked', authorize)
    monkeypatch.setattr(repository.logger, 'error', lambda message,*args:logged.append(message.format(*args)))
    if denied:
        with pytest.raises(PermissionDeniedError) as error:
            repository.vector_rows(lambda:session,[1.0],1,7,10)
        assert error.value is original
        session.execute.assert_not_called()
    else: assert repository.vector_rows(lambda:session,[1.0],1,7,10) is rows
    session.close.assert_called_once(); session.invalidate.assert_called_once()
    assert logged and 'private-' not in str(logged)
