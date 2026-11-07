from typing import Any, cast
# pyright: reportPrivateUsage=false
"""无模型提供商调用的自选真实 PostgreSQL/pgvector 检索测试."""
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.features.file.search_service import _search_files_vector_by_embedding
from app.models.file import File
from app.models.file_chunk import FileChunk
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from entry.init_db import initialize_database
from tests.integration.test_database_init import postgres

__all__ = ["postgres"]

pytestmark = pytest.mark.skipif(os.getenv('RUN_VECTOR_TESTS') != '1', reason='Opt-in isolated pgvector retrieval')


def vector(x: Any, y: Any=0):
    return [float(x), float(y)] + [0.0] * 1022


@pytest.fixture
def indexed(postgres: Any):
    initialize_database(postgres)
    factory = sessionmaker(bind=postgres)
    with factory() as db:
        user = User(username='vector-user', password_hash='synthetic-unused-hash', role='common')
        other_user = User(username='other-vector-user', password_hash='synthetic-unused-hash', role='common')
        db.add_all([user, other_user]); db.flush()
        first = Workspace(name='First', owner_id=user.id)
        second = Workspace(name='Second', owner_id=other_user.id)
        db.add_all([first, second]); db.flush()
        db.add_all([WorkspaceMember(workspace_id=first.id,user_id=user.id,role="viewer"),
                    WorkspaceMember(workspace_id=second.id,user_id=other_user.id,role="viewer")])
        files = [File(name=name, file_path=name, workspace_id=workspace.id, uploader_id=workspace.owner_id,
                      mime_type='text/plain', status='success', vector_info=embedding)
                 for name, workspace, embedding in [('near', first, vector(1)), ('orthogonal', first, vector(0, 1)),
                     ('opposite', first, vector(-1)), ('no-vector', first, None), ('foreign', second, vector(1))]]
        db.add_all(files); db.flush()
        # 历史/不一致分块可以通过独立外键.
        # 检索必须限定连接两边的范围, 不能仅信任分块.
        chunks = [FileChunk(file_id=files[0].id, workspace_id=first.id, chunk_index=0,
                            page_number=2, content='Local near content', vector_info=vector(1)),
                  FileChunk(file_id=files[1].id, workspace_id=first.id, chunk_index=0,
                            content='Local orthogonal content', vector_info=vector(0, 1)),
                  FileChunk(file_id=files[2].id, workspace_id=first.id, chunk_index=0,
                            content='Unindexed local content', vector_info=None),
                  FileChunk(file_id=files[4].id, workspace_id=second.id, chunk_index=0,
                            content='Foreign content', vector_info=vector(1)),
                  FileChunk(file_id=files[4].id, workspace_id=first.id, chunk_index=1,
                            content='INCONSISTENT_FOREIGN_SECRET', vector_info=vector(1)),
                  FileChunk(file_id=files[0].id, workspace_id=second.id, chunk_index=1,
                            content='INCONSISTENT_LOCAL_SECRET', vector_info=vector(1))]
        db.add_all(chunks); db.commit()
        identifiers = first.id, second.id, [file.id for file in files]
    yield factory, *identifiers


def test_file_vector_cosine_pages_and_scope(indexed: Any) -> None:
    factory, first, second, identifiers = indexed
    with factory() as db:
        pages: list[dict[str, Any]] = [_search_files_vector_by_embedding(db, first, vector(1), page, 1) for page in range(1, 5)]
        assert [page['total'] for page in pages] == [3] * 4
        assert [cast(list[dict[str, Any]], page['items'])[0]['id'] for page in pages[:3]] == identifiers[:3]
        assert pages[3]['items'] == []
        assert all(item['workspace_id'] == first for page in pages for item in cast(list[dict[str, Any]], page['items']))
        foreign: dict[str, Any] = _search_files_vector_by_embedding(db, second, vector(1), 1, 10)
        assert foreign['total'] == 1 and [item['id'] for item in cast(list[dict[str, Any]], foreign['items'])] == [identifiers[4]]
        empty: dict[str, Any] = _search_files_vector_by_embedding(db, first, [], 1, 10)
        assert empty['items'] == [] and empty['error'] == 'No embeddings returned'


def test_actual_rag_chunk_join_scope_and_source_metadata(indexed: Any, postgres: Any) -> None:
    _factory, first, second, identifiers = indexed
    # 单元测试 conftest 替换了 LangChain. 在子进程中使用锁定的真实 Document 类
    # 和生产检索函数, 绝非模型端点.
    code = """
import json, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.features.chat import service
from app.models.workspace import Workspace
settings = json.load(sys.stdin)
engine = create_engine(settings['database'])
service.SessionLocal = sessionmaker(bind=engine)
try:
    results = []
    for workspace in settings['workspaces']:
        with service.SessionLocal() as db:
            actor = db.get(Workspace, workspace).owner_id
        docs = service._db_search_by_vector(settings['vector'], 'synthetic query', workspace, 20, actor)
        results.append([{'content': doc.page_content, 'metadata': doc.metadata} for doc in docs])
    print(json.dumps(results))
finally:
    engine.dispose()
"""
    result = subprocess.run([sys.executable, '-c', code], input=json.dumps({
        'database': postgres.url.render_as_string(hide_password=False), 'vector': vector(1), 'workspaces': [first, second]}),
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    docs, other = json.loads(result.stdout)
    assert [doc["metadata"]["id"] for doc in other] == [identifiers[4]]
    assert all("LOCAL_SECRET" not in doc["content"] for doc in other)
    assert [doc['metadata']['id'] for doc in docs] == identifiers[:2]
    assert all('FOREIGN' not in doc['content'] for doc in docs)
    assert docs[0]['metadata']['page_number'] == 2
    assert docs[0]['metadata']['chunk_index'] == 0
    assert docs[0]['metadata']['distance'] == pytest.approx(0)
    assert docs[1]['metadata']['distance'] == pytest.approx(1)
    assert [doc['metadata']['rank'] for doc in docs] == [1, 2]
    assert all(doc['metadata']['retrieval_id'].startswith('chunk:') for doc in docs)


def test_real_cosine_indexes_and_bad_dimension_transaction_rollback(indexed: Any, postgres: Any) -> None:
    factory, first, _second, identifiers = indexed
    with postgres.begin() as db:
        definitions = dict(db.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE indexname IN ('file_vector_idx','file_chunk_vector_idx')")).all())
        assert len(definitions) == 2 and all('vector_cosine_ops' in value for value in definitions.values())
        db.execute(text('SET LOCAL enable_seqscan = off'))
        for table, index in [('files', 'file_vector_idx'), ('file_chunks', 'file_chunk_vector_idx')]:
            plan = '\n'.join(row[0] for row in db.execute(text(f'EXPLAIN SELECT id FROM {table} ORDER BY vector_info <=> CAST(:query AS vector) LIMIT 2'), {'query': str(vector(1))}))
            assert index in plan, plan
    with factory() as db:
        with pytest.raises(Exception):
            db.execute(text('UPDATE files SET vector_info = CAST(:value AS vector) WHERE id=:id'), {'value': '[1,0]', 'id': identifiers[0]})
        db.rollback()
        result: dict[str, Any] = _search_files_vector_by_embedding(db, first, vector(1), 1, 10)
        assert [item['id'] for item in cast(list[dict[str, Any]], result['items'])] == identifiers[:3]



@pytest.mark.parametrize('kind', ['single','multi'])
async def test_chat_retrieval_denies_membership_removed_during_embedding(indexed: Any, postgres: Any, monkeypatch: Any, kind: Any) -> None:
    from app.features.chat import service
    from app.features.chat.query_rewrite import RewriteKeywordDimensions
    from app.exceptions import PermissionDeniedError
    factory, first, _second, _identifiers = indexed
    with factory() as db:
        ws = db.get(Workspace, first)
        assert ws is not None
        actor = ws.owner_id
    monkeypatch.setattr(service, 'SessionLocal', factory)
    class Embeddings:
        async def aembed_query(self, *args: Any, **kwargs: Any):
            assert kwargs['user_id'] == actor
            with postgres.begin() as independent:
                independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=:ws AND user_id=:actor'), {'ws':first,'actor':actor})
            return vector(1)
        async def aembed_documents(self, queries: Any, **kwargs: Any):
            await self.aembed_query('',**kwargs)
            return [vector(1) for _ in queries]
    monkeypatch.setattr(service,'get_embeddings_model',lambda:Embeddings())
    async def forbidden_rerank(*args: Any): raise AssertionError('denied content must not reach reranker')
    monkeypatch.setattr(service,'rerank_documents',forbidden_rerank)
    with pytest.raises(PermissionDeniedError):
        if kind == 'single': await service.custom_db_retriever('private query',first,user_id=actor)
        else:
            dims = RewriteKeywordDimensions(topic_terms=[],entity_terms=[],time_terms=[],file_type_terms=[],action_terms=[],synonym_terms=[])
            await service.multi_query_db_retriever('private query',first,dims,user_id=actor)
    with postgres.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM files')) == 5
        assert db.scalar(text('SELECT count(*) FROM file_chunks')) == 6


def test_chat_chunk_query_holds_current_membership_until_session_close(indexed: Any, postgres: Any, monkeypatch: Any) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError
    from app.features.chat import service
    factory, first, _second, _identifiers = indexed
    with factory() as db:
        ws = db.get(Workspace, first)
        assert ws is not None
        actor = ws.owner_id
    monkeypatch.setattr(service,'SessionLocal',factory)
    observed: list[bool] = []
    def before_query(conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any):
        if 'SELECT c.id' not in statement: return
        with postgres.connect() as independent:
            independent.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError) as error:
                independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=:ws AND user_id=:actor'), {'ws':first,'actor':actor})
            assert getattr(error.value.orig, 'pgcode', None) == '55P03'
            independent.rollback()
        observed.append(True)
    event.listen(postgres,'before_cursor_execute',before_query)
    try: assert len(service._db_search_by_vector(vector(1),'query',first,20,actor)) == 2
    finally: event.remove(postgres,'before_cursor_execute',before_query)
    assert observed == [True]
    with postgres.begin() as independent:
        independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=:ws AND user_id=:actor'), {'ws':first,'actor':actor})


@pytest.mark.parametrize('caller',['service','mcp'])
@pytest.mark.parametrize('change',['allowed','before','after'])
async def test_semantic_search_provider_admission_and_actual_usage(indexed: Any,postgres: Any,monkeypatch: Any,change: Any,caller: Any) -> None:
    from types import SimpleNamespace
    from sqlalchemy import text
    from app.exceptions import PermissionDeniedError
    from app.features.file import search_service
    from app.features.token_usage import service as usage
    from app.infra.llm import client as llm
    factory,first,_second,identifiers=indexed
    with factory() as db:
        ws = db.get(Workspace,first)
        assert ws is not None
        actor = ws.owner_id
    calls: list[str] = []
    def revoke() -> None:
        with postgres.begin() as db:
            db.execute(text('DELETE FROM workspace_members WHERE workspace_id=:workspace AND user_id=:actor'),
                       {'workspace':first,'actor':actor})
    async def embed(**kwargs: Any):
        calls.append('provider')
        if change=='after':revoke()
        return SimpleNamespace(data=[SimpleNamespace(index=0,embedding=vector(1))],
                               usage=SimpleNamespace(total_tokens=12,prompt_tokens=12))
    monkeypatch.setattr(search_service,'get_embedding_model_config',lambda:{'api':'http://provider.test','key':'disposable','model':'embedding'})
    # 仅提供商 SDK 响应为合成数据. 鉴权、pgvector 检索、
    # 回执持久化和用户聚合更新均为原生执行.
    def mock_get_client(*args: Any) -> SimpleNamespace:
        return SimpleNamespace(embeddings=SimpleNamespace(create=embed))
    monkeypatch.setattr(llm,'_get_client',mock_get_client)
    monkeypatch.setattr(usage,'SessionLocal',factory)
    async def search() -> dict[str, Any]:
        if caller=='service':
            return cast(dict[str, Any], await search_service.search_files(None, first, search_service.SearchFilesParams(query='query', search_type='semantic'), session_factory=factory,user_id=actor))
        from app.mcp import server
        from app.mcp.context import McpRequestContext,set_mcp_context,reset_mcp_context
        monkeypatch.setattr(server,'SessionLocal',factory)
        token=set_mcp_context(McpRequestContext(user_id=actor,workspace_id=first,role='viewer'))
        try:return cast(dict[str, Any], json.loads(await server.search_files('query',search_type='vector')))
        finally:reset_mcp_context(token)
    if change=='before':revoke()
    if change=='allowed':
        result=await search()
        assert result['total']==3 and cast(list[dict[str, Any]], result['items'])[0]['id']==identifiers[0]
        assert all(item['workspace_id']==first for item in cast(list[dict[str, Any]], result['items']))
    else:
        with pytest.raises((PermissionDeniedError,PermissionError)):
            await search()
    assert calls==([] if change=='before' else ['provider'])
    with postgres.connect() as db:
        logs=db.execute(text('SELECT user_id,total_tokens FROM token_usage_logs')).all()
        assert logs==([] if change=='before' else [(actor,12)])
        assert db.scalar(text('SELECT total_tokens FROM users WHERE id=:actor'),{'actor':actor})==(0 if change=='before' else 12)


@pytest.mark.parametrize('kind',['search','status','mcp-write'])
async def test_cancelled_owned_sql_drains_native_lock_before_return(indexed: Any,postgres: Any,monkeypatch: Any,kind: Any) -> None:
    import asyncio
    import threading
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.features.file import search_service,service
    from app.mcp.context import reset_mcp_context
    factory,first,_,_=indexed
    with factory() as db:
        ws = db.get(Workspace,first)
        assert ws is not None
        actor = ws.owner_id
    pids: list[int] = []
    closed = threading.Event()
    class OwnedSession(Session):
        def __init__(self,*args: Any,**kwargs: Any):
            super().__init__(*args,**kwargs)
            pids.append(int(self.execute(text('SELECT pg_backend_pid()')).scalar_one()))
        def close(self):
            try:super().close()
            finally:closed.set()
    owned=sessionmaker(bind=postgres,class_=OwnedSession)
    table='folder' if kind=='mcp-write' else 'files'
    gate=postgres.connect();transaction=gate.begin()
    token=None
    if kind=='mcp-write':
        from app.mcp import server,audit
        from app.mcp.context import McpRequestContext,set_mcp_context
        from app.models.folder import Folder
        with factory() as db:
            root=Folder(name='Root',workspace_id=first,parent_id=None)
            db.add(root);db.commit();root_id=root.id
        with postgres.begin() as db:
            db.execute(text("UPDATE workspace_members SET role='editor' WHERE workspace_id=:workspace AND user_id=:actor"),
                       {'workspace':first,'actor':actor})
        monkeypatch.setattr(server,'SessionLocal',owned)
        monkeypatch.setattr(audit,'SessionLocal',factory)
        token=set_mcp_context(McpRequestContext(actor,first,'editor'))
        operation=server.create_folder('accepted-before-cancel',parent_id=root_id)
    elif kind=='search':
        operation=search_service.search_files(None, first, search_service.SearchFilesParams(query='unique-native-held-query'), session_factory=owned, user_id=actor)
    else:operation=service.process_status(None,first,session_factory=owned,user_id=actor)
    pending=None
    try:
        gate.execute(text("SET LOCAL lock_timeout='2s'"))
        gate.execute(text('LOCK TABLE '+table+' IN ACCESS EXCLUSIVE MODE'))
        pending=asyncio.create_task(operation)
        deadline=asyncio.get_running_loop().time()+2
        while True:
            with postgres.connect() as observer:
                waiting = bool(pids) and bool(observer.scalar(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"),{'pid':pids[0]}))
            if waiting:break
            assert asyncio.get_running_loop().time()<deadline,'native SQL never reached the held table lock'
            await asyncio.sleep(.01)
        pending.cancel('native-caller-stop');await asyncio.sleep(.01)
        pending.cancel('repeat-stop');await asyncio.sleep(.01)
        assert not pending.done() and not closed.is_set()
    finally:
        transaction.rollback();gate.close()
        if pending is not None:
            with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,2)
        else:operation.close()
        if token is not None:reset_mcp_context(token)
    assert closed.is_set()
    if kind=='mcp-write':
        with postgres.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM folder WHERE workspace_id=:workspace AND name='accepted-before-cancel'"),{'workspace':first})==1
    # 已启动的 SQL 操作在取消返回前完成;
    # 不断言数据库端取消或回滚已接受的写入.


@pytest.mark.parametrize('kind', ['single', 'multi'])
async def test_chat_cancelled_native_sql_closes_all_owned_sessions(indexed: Any, postgres: Any, monkeypatch: Any, kind: Any) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sqlalchemy.orm import Session
    from app.features.chat import service
    from app.features.chat.query_rewrite import RewriteKeywordDimensions

    factory, workspace, _, _ = indexed
    with factory() as db:
        ws = db.get(Workspace, workspace)
        assert ws is not None
        actor = ws.owner_id
    pids: list[int] = []
    closed: list[int] = []

    class OwnedSession(Session):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.pid = int(self.execute(text('SELECT pg_backend_pid()')).scalar_one())
            pids.append(self.pid)

        def close(self):
            try:
                super().close()
            finally:
                closed.append(self.pid)

    owned = sessionmaker(bind=postgres, class_=OwnedSession)
    monkeypatch.setattr(service, 'SessionLocal', owned)
    # 提供商响应为合成数据; 生产成员身份与 pgvector SQL
    # 在原生数据库上执行, 包括并行标识行锁.
    monkeypatch.setattr(service, 'get_embeddings_model', lambda: SimpleNamespace(
        aembed_query=AsyncMock(return_value=vector(1)),
        aembed_documents=AsyncMock(return_value=[vector(1), vector(1), vector(1)]),
    ))
    def mock_build_multi_queries(*args: Any, **kwargs: Any) -> list[str]:
        return ['one', 'two', 'three']
    monkeypatch.setattr(service, 'build_multi_queries', mock_build_multi_queries)
    if kind == 'single':
        operation = service.custom_db_retriever('query', workspace, user_id=actor)
        expected = 1
    else:
        dims = RewriteKeywordDimensions(topic_terms=[], entity_terms=[], time_terms=[], file_type_terms=[], action_terms=[], synonym_terms=[])
        operation = service.multi_query_db_retriever('query', workspace, dims, user_id=actor)
        expected = 3
    table = 'file_chunks'
    gate = postgres.connect()
    transaction = gate.begin()
    pending = None
    try:
        gate.execute(text("SET LOCAL lock_timeout='2s'"))
        gate.execute(text('LOCK TABLE ' + table + ' IN ACCESS EXCLUSIVE MODE'))
        pending = asyncio.create_task(operation)
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            with postgres.connect() as observer:
                waiting = observer.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid=ANY(:pids) AND wait_event_type='Lock'"
                ), {'pids': pids}) if pids else 0
            if len(pids) == expected and waiting == expected:
                break
            assert asyncio.get_running_loop().time() < deadline, 'chat SQL never reached native locks'
            await asyncio.sleep(.01)
        pending.cancel('native-caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and not closed
    finally:
        transaction.rollback()
        gate.close()
        if pending is not None:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
        else:
            operation.close()
    assert len(closed) == expected and sorted(closed) == sorted(pids)
