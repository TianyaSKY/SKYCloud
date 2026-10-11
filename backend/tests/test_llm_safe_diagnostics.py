"""Failure logs must not serialize provider responses, exception text or SQL binds."""
import json
from unittest.mock import Mock, MagicMock
import httpx
import pytest
from loguru import logger

PRIVATE='private-prompt-provider-key-sentinel'

@pytest.fixture
def logs():
    records=[]
    sink=logger.add(lambda message:records.append((str(message),message.record['exception'])),format='{message}',level='DEBUG')
    try:yield records
    finally:logger.remove(sink)

def assert_safe(logs,diagnostic):
    rendered=''.join(row[0] for row in logs)
    assert diagnostic in rendered
    assert PRIVATE not in rendered
    assert all(exception is None for _,exception in logs)


def test_accounting_sql_exception_cannot_log_bound_prompt(monkeypatch,logs):
    from sqlalchemy.exc import StatementError
    from app.features.token_usage import service
    session=Mock()
    session.execute.side_effect=StatementError(PRIVATE,'INSERT INTO usage VALUES (:prompt)',{'prompt':PRIVATE},ValueError(PRIVATE))
    monkeypatch.setattr(service,'SessionLocal',lambda:session)
    service.record_usage(1,'chat',query_summary=PRIVATE,extra_info=PRIVATE)
    session.rollback.assert_called_once();session.close.assert_called_once()
    assert_safe(logs,'error_type=StatementError')


def test_best_effort_accounting_does_not_log_action_or_exception(monkeypatch,logs):
    from app.infra.llm.usage import safe_record
    from app.features.token_usage import service
    monkeypatch.setattr(service,'record_usage',Mock(side_effect=RuntimeError(PRIVATE)))
    safe_record(1,PRIVATE,None,1,2,3,query_summary=PRIVATE)
    assert_safe(logs,'error_type=RuntimeError')


def test_shutdown_failure_still_closes_remaining_clients_without_private_detail(monkeypatch,logs):
    from app.infra.llm import sync_client
    failed=Mock();failed.close.side_effect=RuntimeError(PRIVATE)
    other=Mock()
    monkeypatch.setattr(sync_client,'_client_cache',{('api','private'):failed,('api','other'):other})
    sync_client.close_sync_clients()
    failed.close.assert_called_once();other.close.assert_called_once()
    assert sync_client._client_cache=={}
    assert_safe(logs,'error_type=RuntimeError')


@pytest.mark.parametrize('body',[{'error':PRIVATE},[PRIVATE],None,{'embeddings':PRIVATE}])
async def test_embed_malformed_shape_never_logs_provider_body(monkeypatch,logs,body):
    from app.infra.llm import remote_embed
    client_class=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request:httpx.Response(200,content=json.dumps(body)))
    monkeypatch.setattr(remote_embed.httpx,'AsyncClient',lambda **kw:client_class(transport=transport,**kw))
    assert await remote_embed.RemoteEmbedder('http://embedding.test').process([PRIVATE]) is None
    assert_safe(logs,'invalid format')

@pytest.mark.parametrize('failure',['http','json','transport'])
async def test_embed_http_json_and_network_failures_have_safe_diagnostics(monkeypatch,logs,failure):
    from app.infra.llm import remote_embed
    client_class=httpx.AsyncClient
    def respond(request):
        if failure=='transport':raise httpx.RequestError(PRIVATE,request=request)
        return httpx.Response(503 if failure=='http' else 200,text=PRIVATE)
    transport=httpx.MockTransport(respond)
    monkeypatch.setattr(remote_embed.httpx,'AsyncClient',lambda **kw:client_class(transport=transport,**kw))
    assert await remote_embed.RemoteEmbedder('http://embedding.test').process([PRIVATE]) is None
    assert_safe(logs,'error_type=')


async def test_rerank_failure_returns_original_docs_and_keeps_exception_private(monkeypatch,logs):
    from app.features.chat import rerank
    transport=httpx.MockTransport(lambda request:httpx.Response(503,text=PRIVATE))
    async with httpx.AsyncClient(transport=transport) as client:
        monkeypatch.setattr(rerank,'_get_rerank_client',lambda:client)
        monkeypatch.setattr(rerank,'get_rerank_model_config',lambda:{'api':'http://rerank.test','key':PRIVATE,'model':'model'})
        monkeypatch.setattr(rerank,'get_rerank_top_k',lambda:2)
        docs=[MagicMock(page_content=PRIVATE),MagicMock(page_content='other')]
        assert await rerank.rerank_documents(PRIVATE,docs) is docs
    assert_safe(logs,'error_type=HTTPStatusError')


async def test_chat_stream_failure_keeps_prompt_out_of_logs_and_sse(monkeypatch,logs):
    from app.features.chat import service
    from app.infra.llm import client
    chain=MagicMock()
    chain.__or__.return_value=chain;chain.__ror__.return_value=chain
    chain.with_config.return_value=chain;chain.with_structured_output.return_value=chain
    async def events(*args,**kwargs):
        raise RuntimeError(PRIVATE)
        yield  # An async iterator that fails on its first read.
    chain.astream_events=events
    monkeypatch.setattr(service,'get_chat_model',lambda:chain)
    monkeypatch.setattr(service.ChatPromptTemplate,'from_template',lambda *a:chain)
    for name in ('RunnableParallel','RunnableLambda','StrOutputParser'):
        monkeypatch.setattr(service,name,lambda *a,**kw:chain)
    recorder=Mock();monkeypatch.setattr(client,'record_llm_usage',recorder)
    output=''.join([event async for event in service.generate_chat_events(1,2,PRIVATE,[])])
    event = json.loads(output.removeprefix('data: ').strip())
    assert event['type'] == 'error' and '对话请求失败' in event['payload']['message'] and PRIVATE not in output
    recorder.assert_called_once()
    assert_safe(logs,'error_type=RuntimeError')
