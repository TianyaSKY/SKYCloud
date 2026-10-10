import asyncio
import gzip
import tracemalloc
import zlib
import httpx
import pytest
from app.infra.http_decode import decoded_bytes, CHUNK_BYTES


class Stream(httpx.AsyncByteStream):
    def __init__(self, data, split=None):
        self.data=data;self.split=split or len(data);self.closed=False
    async def __aiter__(self):
        for i in range(0,len(self.data),self.split):yield self.data[i:i+self.split]
    async def aclose(self):self.closed=True


@pytest.mark.parametrize('encoding,raw', [('gzip',False),('deflate',False),('deflate',True)])
@pytest.mark.parametrize('split',[1,None])
async def test_bounded_decode_matches_original(encoding,raw,split):
    body=b'data: valid utf8 '+bytes(range(256))*1000
    compressor=zlib.compressobj(wbits=-15 if raw else 15)
    encoded=gzip.compress(body) if encoding=='gzip' else compressor.compress(body)+compressor.flush()
    response=httpx.Response(200,headers={'content-encoding':encoding},stream=Stream(encoded,split))
    try:
        chunks=[chunk async for chunk in decoded_bytes(response,len(body))]
        assert b''.join(chunks)==body
        assert max(map(len,chunks))<=CHUNK_BYTES
    finally:await response.aclose()


@pytest.mark.parametrize('encoding',['gzip','deflate'])
async def test_compressed_bomb_never_allocates_full_output(encoding):
    # Create the fixture before tracing. One tiny wire chunk expands to 16 MiB.
    body=b'x'*(16*1024*1024)
    wire=gzip.compress(body) if encoding=='gzip' else zlib.compress(body)
    response=httpx.Response(200,headers={'content-encoding':encoding},stream=Stream(wire))
    forwarded=0
    tracemalloc.start()
    try:
        with pytest.raises(httpx.DecodingError,match='decoded response too large'):
            async for chunk in decoded_bytes(response,128*1024):
                forwarded+=len(chunk)
        _,peak=tracemalloc.get_traced_memory()
        assert forwarded<=128*1024
        assert peak<1024*1024
    finally:
        tracemalloc.stop();await response.aclose()


@pytest.mark.parametrize('wire',[b'bad',gzip.compress(b'valid')[:-1]])
async def test_invalid_or_truncated_compression_fails(wire):
    response=httpx.Response(200,headers={'content-encoding':'gzip'},stream=Stream(wire))
    with pytest.raises(httpx.DecodingError):
        async for _ in decoded_bytes(response,100):pass
    await response.aclose()


@pytest.mark.parametrize('encoding',['br','gzip, deflate','private-invalid'])
async def test_unnegotiated_encoding_not_read(encoding):
    class Unread(httpx.AsyncByteStream):
        async def __aiter__(self):raise AssertionError('body must not be decoded');yield b''
    response=httpx.Response(200,headers={'content-encoding':encoding},stream=Unread())
    with pytest.raises(httpx.DecodingError,match='Unsupported upstream'):
        async for _ in decoded_bytes(response,100):pass
    await response.aclose()


@pytest.mark.parametrize('kind',['model-buffered','model-stream','ui'])
@pytest.mark.parametrize('mode',['valid','oversize','unsupported'])
async def test_gateway_compression_admission_and_owned_release(monkeypatch,kind,mode):
    import json
    import time
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.features.runtime_llm import router as model
    from app.features.workspace import ui_gateway,ui_tickets
    clients,responses=[],[]
    body=b'{"allowed":true}'
    wire=gzip.compress(body if mode=='valid' else b'x'*4096)
    actual_client=httpx.AsyncClient
    def provider(request):
        assert request.headers['accept-encoding']=='gzip, deflate, identity'
        stream=Stream(wire)
        response=httpx.Response(200,headers={'content-encoding':'gzip' if mode!='unsupported' else 'br'},stream=stream)
        responses.append((response,stream));return response
    def client(**kwargs):
        opened=actual_client(transport=httpx.MockTransport(provider),**kwargs)
        clients.append(opened);return opened
    async def check(*args,**kwargs):pass
    messages=[{'type':'http.request','body':json.dumps({'model':'allowed','messages':[{'role':'user','content':'hi'}],
               'stream':kind=='model-stream'}).encode() if kind!='ui' else b'', 'more_body':False}]
    async def receive():
        if messages:return messages.pop()
        await asyncio.Event().wait()
    request=Request({'type':'http','method':'POST' if kind!='ui' else 'GET','path':'/','query_string':b'',
                     'headers':[(b'host',b'runtime.test'),(b'cookie',(ui_tickets.COOKIE+'=opaque').encode())]},receive)
    if kind=='ui':
        def authorize(*args):return {'expires_at':time.time()+60,'container_id':'a'*64,'origin':'http://runtime.test'}
        monkeypatch.setattr(ui_gateway,'authenticate_owned',authorize)
        monkeypatch.setattr(ui_gateway,'reauthenticate',authorize)
        monkeypatch.setattr(ui_gateway.httpx,'AsyncClient',client)
        monkeypatch.setattr(ui_gateway,'MAX_RESPONSE',1024)
        operation=ui_gateway.relay('event',request,None)
    else:
        monkeypatch.setattr(model,'upstream_client',client)
        monkeypatch.setattr(model,'_validate_current_credential',lambda *args,**kwargs:{'model':'allowed'})
        monkeypatch.setattr(model,'current_credential',check)
        monkeypatch.setattr(model,'verify_connected',check)
        monkeypatch.setattr(model.quota,'reserve',lambda *args:None)
        monkeypatch.setattr(model,'get_chat_model_config',lambda:{'key':'private-key','api':'http://provider.test'})
        monkeypatch.setattr(model,'MAX_RESPONSE_BYTES',1024)
        operation=model.chat_completions(request,{'model':'allowed'},None)
    if mode=='unsupported' or (mode=='oversize' and kind=='model-buffered'):
        with pytest.raises(HTTPException) as error:await operation
        assert error.value.status_code==502
    else:
        response=await operation
        if kind=='model-buffered':output=response.body
        else:
            sent=[]
            async def send(message):
                if message['type']=='http.response.body':sent.append(message.get('body',b''))
            await response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send)
            output=b''.join(sent)
        assert output==(body if mode=='valid' else b'')
    assert all(c.is_closed for c in clients)
    assert all(r.is_closed and s.closed for r,s in responses)
