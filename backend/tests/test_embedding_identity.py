"""Exercise the real cached LangChain wrapper/SDK outside the mocked test process."""
import os
from pathlib import Path
import subprocess
import sys


PROBE = r'''
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.models.user import User
from app.models.token_usage_log import TokenUsageLog
from app.features.token_usage import service as usage
from app.features.chat import service as rag
from app.infra.llm.client import close_llm_clients
from langchain_core.runnables import RunnableLambda
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from app.features.chat.query_rewrite import RewriteKeywordDimensions

barrier = threading.Barrier(2)
cancel_started = threading.Event()
cancel_release = threading.Event()
calls = []
call_lock = threading.Lock()

class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == '/v1/embeddings'
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        inputs = payload['input']
        with call_lock:
            calls.append(inputs)
        if inputs in (['one'], ['two']):
            barrier.wait(timeout=5)
        if inputs == ['cancelled']:
            cancel_started.set()
            assert cancel_release.wait(timeout=5)
        tokens = 22 if inputs[0].startswith('two') else 11
        response = {'object': 'list', 'model': 'synthetic-embedding',
            'data': [{'object': 'embedding', 'index': i, 'embedding': [0.1] * 1024}
                     for i in range(len(inputs))],
            'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}
        body = json.dumps(response).encode()
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
thread = threading.Thread(target=provider.serve_forever, daemon=True)
thread.start()
os.environ.update(EMB_API_URL=f'http://127.0.0.1:{provider.server_port}/v1',
                  EMB_API_KEY='synthetic-only', EMB_MODEL_NAME='synthetic-embedding')
engine = create_engine('sqlite:///' + sys.argv[1])
User.__table__.create(engine)
TokenUsageLog.__table__.create(engine)
usage.SessionLocal = sessionmaker(bind=engine)
with usage.SessionLocal() as db:
    db.add_all([User(id=i, username=f'user-{i}', password_hash='synthetic') for i in (1,2,3)])
    db.commit()

# Keep the SQL retrieval adapter out of this identity/accounting regression.
# Embedding requests and accounting Session/transactions are real.
rag._db_search_by_vector = lambda *args: []
shared = rag.get_embeddings_model()

class Model:
    def with_structured_output(self, *args, **kwargs):
        def rewrite(prompt):
            question = prompt.to_string().rsplit('问题：', 1)[1].strip()
            return RewriteKeywordDimensions(topic_terms=[question + '-extra'],
                entity_terms=[], time_terms=[], file_type_terms=[],
                action_terms=[], synonym_terms=[])
        return RunnableLambda(rewrite)

    def with_config(self, config):
        return FakeListChatModel(responses=['synthetic answer']).with_config(config)

rag.get_chat_model = lambda: Model()

async def request(user_id, word):
    assert rag.get_embeddings_model() is shared
    events = [json.loads(chunk.split('data:', 1)[1])
              async for chunk in rag.generate_chat_events(user_id, user_id, word, [])]
    assert any(event['type'] == 'keywords' for event in events), events
    assert ''.join(event['content'] for event in events if event['type'] == 'token') == 'synthetic answer', events

async def main():
    await asyncio.gather(request(1, 'one'), request(2, 'two'))
    with usage.SessionLocal() as db:
        rows = db.scalars(select(TokenUsageLog)).all()
        assert Counter(row.user_id for row in rows) == {1:2, 2:2}, [(row.user_id, row.total_tokens) for row in rows]
    cancelled = asyncio.create_task(shared.aembed_query('cancelled', user_id=3))
    try:
        assert await asyncio.to_thread(cancel_started.wait, 5)
        cancelled.cancel()
        try:
            await cancelled
            raise AssertionError('Cancelled request unexpectedly completed')
        except asyncio.CancelledError:
            pass
    finally:
        cancel_release.set()
    assert len(await shared.aembed_query('one-after-cancel', user_id=1)) == 1024
    await close_llm_clients()

try:
    asyncio.run(main())
    with usage.SessionLocal() as db:
        rows = db.scalars(select(TokenUsageLog)).all()
        assert Counter(row.user_id for row in rows) == {1:3, 2:2}
        assert sorted((row.user_id, row.total_tokens) for row in rows) == [(1,11)]*3 + [(2,22)]*2
        assert db.get(User,1).total_tokens == 33
        assert db.get(User,2).total_tokens == 44
        assert db.get(User,3).total_tokens == 0
    assert len(calls) == 6
    assert not hasattr(shared, 'set_tracking_user')
    print('CACHED_EMBEDDING_IDENTITY_OK')
finally:
    cancel_release.set()
    provider.shutdown()
    provider.server_close()
    thread.join(timeout=5)
    engine.dispose()
'''


def test_concurrent_cached_embeddings_keep_user_usage_separate(tmp_path):
    result = subprocess.run(
        [sys.executable, '-c', PROBE, str(tmp_path / 'usage.sqlite')],
        cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'CACHED_EMBEDDING_IDENTITY_OK' in result.stdout
