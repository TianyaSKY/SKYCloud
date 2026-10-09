"""Semantic search must authorize before provider dispatch and attribute usage."""
import threading
from types import SimpleNamespace
import pytest
from app.exceptions import PermissionDeniedError
from app.features.file import search_service
from app.infra.llm import client as llm


@pytest.mark.parametrize('revoked',[False,True])
async def test_semantic_search_authorization_precedes_embedding_and_usage(monkeypatch,revoked):
    loop_thread=threading.get_ident()
    events=[]
    class Session:
        def __enter__(self):assert threading.get_ident()!=loop_thread;return self
        def __exit__(self,*args):pass
    def authorize(session,workspace_id,user_id):
        assert threading.get_ident()!=loop_thread
        assert (workspace_id,user_id)==(11,7)
        events.append('authorize')
        if revoked:raise PermissionDeniedError('revoked')
    async def embed(**kwargs):
        events.append('provider')
        return SimpleNamespace(data=[SimpleNamespace(index=0,embedding=[1.0]*1024)],
                               usage=SimpleNamespace(total_tokens=12,prompt_tokens=12))
    def record(**usage):
        assert threading.get_ident()!=loop_thread
        events.append(('usage',usage['user_id'],usage['total_tokens']))
    def retrieve(session,*args):return {'items':[],'total':0}
    monkeypatch.setattr(search_service,'assert_member',authorize)
    monkeypatch.setattr(search_service,'get_embedding_model_config',lambda:{'api':'http://provider.test','key':'disposable','model':'embedding'})
    monkeypatch.setattr(llm,'_get_client',lambda *args:SimpleNamespace(embeddings=SimpleNamespace(create=embed)))
    monkeypatch.setattr(llm,'_safe_record',record)
    monkeypatch.setattr(search_service,'_search_files_vector_by_embedding',retrieve)
    if revoked:
        with pytest.raises(PermissionDeniedError):
            await search_service.search_files(None,11,'query',search_type='semantic',session_factory=Session,user_id=7)
        assert events==['authorize']
    else:
        assert await search_service.search_files(None,11,'query',search_type='semantic',session_factory=Session,user_id=7)=={'items':[],'total':0}
        assert events==['authorize','provider',('usage',7,12),'authorize']
