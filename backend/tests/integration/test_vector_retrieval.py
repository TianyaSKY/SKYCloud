"""Opt-in real PostgreSQL/pgvector retrieval with no model-provider calls."""
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.features.file.service import _search_files_vector_by_embedding
from app.models.file import File
from app.models.file_chunk import FileChunk
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from entry.migrate import upgrade
from tests.integration.test_migrations import postgres  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RUN_VECTOR_TESTS') != '1', reason='Opt-in isolated pgvector retrieval')


def vector(x, y=0):
    return [float(x), float(y)] + [0.0] * 1022


@pytest.fixture
def indexed(postgres):
    upgrade(postgres)
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
        # Historical/inconsistent chunks can pass the independent foreign keys.
        # Retrieval must scope both sides of the join, not trust only the chunk.
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
        yield factory, first.id, second.id, [file.id for file in files]


def test_file_vector_cosine_pages_and_scope(indexed):
    factory, first, second, identifiers = indexed
    with factory() as db:
        pages = [_search_files_vector_by_embedding(db, first, vector(1), page, 1) for page in range(1, 5)]
        assert [page['total'] for page in pages] == [3] * 4
        assert [page['items'][0]['id'] for page in pages[:3]] == identifiers[:3]
        assert pages[3]['items'] == []
        assert all(item['workspace_id'] == first for page in pages for item in page['items'])
        foreign = _search_files_vector_by_embedding(db, second, vector(1), 1, 10)
        assert foreign['total'] == 1 and [item['id'] for item in foreign['items']] == [identifiers[4]]
        empty = _search_files_vector_by_embedding(db, first, [], 1, 10)
        assert empty['items'] == [] and empty['error'] == 'No embeddings returned'


def test_actual_rag_chunk_join_scope_and_source_metadata(indexed, postgres):
    factory, first, second, identifiers = indexed
    # Unit conftest replaces LangChain. Use the locked real Document class and
    # production retrieval function in a child process, never a model endpoint.
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


def test_real_cosine_indexes_and_bad_dimension_transaction_rollback(indexed, postgres):
    factory, first, second, identifiers = indexed
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
        result = _search_files_vector_by_embedding(db, first, vector(1), 1, 10)
        assert [item['id'] for item in result['items']] == identifiers[:3]


def test_legacy_native_user_role_upgrade_preserves_data_and_rolls_back_failure(postgres):
    from sqlalchemy import event
    from app.infra.schema import check_schema
    upgrade(postgres, '0005_upload_quotas')
    with postgres.begin() as db:
        db.execute(text("CREATE TYPE user_roles AS ENUM ('admin', 'common')"))
        db.execute(text('ALTER TABLE users ALTER COLUMN role DROP DEFAULT'))
        db.execute(text('ALTER TABLE users ALTER COLUMN role TYPE user_roles USING role::user_roles'))
        db.execute(text("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'common'::user_roles"))
        db.execute(text("INSERT INTO users (username,password_hash,role) VALUES ('legacy-admin','synthetic','admin'), ('legacy-null','synthetic',NULL)"))
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('ALTER TABLE users ALTER COLUMN role TYPE VARCHAR'):
            raise RuntimeError('synthetic role migration failure')
    event.listen(postgres, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic role migration failure'):
            upgrade(postgres)
    finally:
        event.remove(postgres, 'before_cursor_execute', fail)
    with postgres.connect() as db:
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0005_upload_quotas'
        assert db.scalar(text("SELECT udt_name FROM information_schema.columns WHERE table_name='users' AND column_name='role'")) == 'user_roles'
        assert 'user_roles' in db.scalar(text("SELECT column_default FROM information_schema.columns WHERE table_name='users' AND column_name='role'"))
    upgrade(postgres)
    check_schema(postgres)
    with postgres.connect() as db:
        assert db.scalar(text("SELECT data_type FROM information_schema.columns WHERE table_name='users' AND column_name='role'")) == 'character varying'
        assert db.execute(text("SELECT username,role FROM users ORDER BY username")).all() == [('legacy-admin', 'admin'), ('legacy-null', None)]
    with sessionmaker(bind=postgres)() as db:
        db.add_all([User(username='bulk-common', password_hash='synthetic', role='common'),
                    User(username='bulk-admin', password_hash='synthetic', role='admin')])
        db.commit()
        assert {user.role for user in db.query(User).filter(User.username.like('bulk-%')).all()} == {'common', 'admin'}
    upgrade(postgres)  # Already at head: idempotent.


@pytest.mark.parametrize('kind', ['single','multi'])
async def test_chat_retrieval_denies_membership_removed_during_embedding(indexed, postgres, monkeypatch, kind):
    from app.features.chat import service
    from app.features.chat.query_rewrite import RewriteKeywordDimensions
    from app.exceptions import PermissionDeniedError
    factory, first, second, identifiers = indexed
    with factory() as db: actor = db.get(Workspace,first).owner_id
    monkeypatch.setattr(service, 'SessionLocal', factory)
    class Embeddings:
        async def aembed_query(self, *args, **kwargs):
            assert kwargs['user_id'] == actor
            with postgres.begin() as independent:
                independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=:ws AND user_id=:actor'), {'ws':first,'actor':actor})
            return vector(1)
        async def aembed_documents(self, queries, **kwargs):
            await self.aembed_query('',**kwargs)
            return [vector(1) for query in queries]
    monkeypatch.setattr(service,'get_embeddings_model',lambda:Embeddings())
    async def forbidden_rerank(*args): raise AssertionError('denied content must not reach reranker')
    monkeypatch.setattr(service,'rerank_documents',forbidden_rerank)
    with pytest.raises(PermissionDeniedError):
        if kind == 'single': await service.custom_db_retriever('private query',first,user_id=actor)
        else:
            dims = RewriteKeywordDimensions(topic_terms=[],entity_terms=[],time_terms=[],file_type_terms=[],action_terms=[],synonym_terms=[])
            await service.multi_query_db_retriever('private query',first,dims,user_id=actor)
    with postgres.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM files')) == 5
        assert db.scalar(text('SELECT count(*) FROM file_chunks')) == 6


def test_chat_chunk_query_holds_current_membership_until_session_close(indexed, postgres, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError
    from app.features.chat import service
    factory, first, second, identifiers = indexed
    with factory() as db: actor = db.get(Workspace,first).owner_id
    monkeypatch.setattr(service,'SessionLocal',factory)
    observed=[]
    def before_query(conn, cursor, statement, parameters, context, executemany):
        if 'SELECT c.id' not in statement: return
        with postgres.connect() as independent:
            independent.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError) as error:
                independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=:ws AND user_id=:actor'), {'ws':first,'actor':actor})
            assert error.value.orig.pgcode == '55P03'
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
async def test_semantic_search_provider_admission_and_actual_usage(indexed,postgres,monkeypatch,change,caller):
    from types import SimpleNamespace
    from sqlalchemy import text
    from app.exceptions import PermissionDeniedError
    from app.features.file import search_service
    from app.features.token_usage import service as usage
    from app.infra.llm import client as llm
    factory,first,second,identifiers=indexed
    with factory() as db:actor=db.get(Workspace,first).owner_id
    calls=[]
    def revoke():
        with postgres.begin() as db:
            db.execute(text('DELETE FROM workspace_members WHERE workspace_id=:workspace AND user_id=:actor'),
                       {'workspace':first,'actor':actor})
    async def embed(**kwargs):
        calls.append('provider')
        if change=='after':revoke()
        return SimpleNamespace(data=[SimpleNamespace(index=0,embedding=vector(1))],
                               usage=SimpleNamespace(total_tokens=12,prompt_tokens=12))
    monkeypatch.setattr(search_service,'get_embedding_model_config',lambda:{'api':'http://provider.test','key':'disposable','model':'embedding'})
    # Only the provider SDK response is synthetic. Authorization, pgvector
    # retrieval, receipt persistence and user aggregate update stay native.
    monkeypatch.setattr(llm,'_get_client',lambda *args:SimpleNamespace(embeddings=SimpleNamespace(create=embed)))
    monkeypatch.setattr(usage,'SessionLocal',factory)
    async def search():
        if caller=='service':
            return await search_service.search_files(None,first,'query',search_type='semantic',session_factory=factory,user_id=actor)
        from app.mcp import server
        from app.mcp.context import McpRequestContext,set_mcp_context,reset_mcp_context
        monkeypatch.setattr(server,'SessionLocal',factory)
        token=set_mcp_context(McpRequestContext(user_id=actor,workspace_id=first,role='viewer'))
        try:return json.loads(await server.search_files('query',search_type='vector'))
        finally:reset_mcp_context(token)
    if change=='before':revoke()
    if change=='allowed':
        result=await search()
        assert result['total']==3 and result['items'][0]['id']==identifiers[0]
        assert all(item['workspace_id']==first for item in result['items'])
    else:
        with pytest.raises((PermissionDeniedError,PermissionError)):
            await search()
    assert calls==([] if change=='before' else ['provider'])
    with postgres.connect() as db:
        logs=db.execute(text('SELECT user_id,total_tokens FROM token_usage_logs')).all()
        assert logs==([] if change=='before' else [(actor,12)])
        assert db.scalar(text('SELECT total_tokens FROM users WHERE id=:actor'),{'actor':actor})==(0 if change=='before' else 12)
