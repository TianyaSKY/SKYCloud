"""Standalone rebuilt Controller and native HTTP; engine target is a synthetic seam."""
import os
import time
import uuid
import httpx
import pytest
from tests.integration.test_runtime_security import docker

pytestmark=pytest.mark.skipif(os.getenv('RUN_CONTROLLER_DIAGNOSTICS')!='1',reason='Opt-in rebuilt Controller boundary')


@pytest.fixture
def relay_controller():
    name='skycloud-controller-decode-'+uuid.uuid4().hex[:12]
    script='''
import gzip,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import uvicorn
from runtime_controller import app as controller_app
from runtime_controller.policy import Settings
class Provider(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.headers.get('Accept-Encoding') == 'gzip, deflate, identity'
        event='/event/' in self.path
        mode=self.path.rsplit('/',1)[-1]
        output=b'data: valid\\n\\n' if event else b'{"healthy":true}'
        wire=gzip.compress(output if mode=='valid' else b'x'*4096)
        if mode=='truncated':wire=wire[:-1]
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream' if event else 'application/json')
        self.send_header('Content-Encoding','br' if mode=='unsupported' else 'gzip')
        self.send_header('Content-Length',str(len(wire)))
        self.end_headers();self.wfile.write(wire)
    def log_message(self,*args):pass
threading.Thread(target=ThreadingHTTPServer(('127.0.0.1',9100),Provider).serve_forever,daemon=True).start()
class Engine:
    def target(self,ref):return 'http://127.0.0.1:9100',('server','disposable')
    def reconcile_retired(self):pass
settings=Settings('T'*48,'S'*48,'test/runtime@sha256:'+'a'*64,'test', '1g',100000,'test','/state')
controller_app.MAX_RESPONSE=1024
uvicorn.run(controller_app.create_app(settings,Engine()),host='0.0.0.0',port=9101,log_level='warning')
'''
    try:
        docker('run','-d','--name',name,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
               '-p','127.0.0.1::9101','--entrypoint','python',os.environ['SKYCLOUD_TEST_CONTROLLER_IMAGE'],'-u','-c',script)
        port=docker('port',name,'9101/tcp').stdout.strip().rsplit(':',1)[1]
        url='http://127.0.0.1:'+port
        headers={'X-Runtime-Controller-Token':'T'*48}
        deadline=time.monotonic()+10
        with httpx.Client(trust_env=False,timeout=1) as client:
            while True:
                try:
                    if client.get(url+'/health',headers=headers).status_code==200:break
                except httpx.HTTPError:pass
                assert time.monotonic()<deadline,'standalone Controller startup failed'
                time.sleep(.05)
        yield name,url,headers
    finally:docker('rm','-f','-v',name,check=False)


def test_native_controller_compression_routes(relay_controller):
    _,url,headers=relay_controller
    with httpx.Client(trust_env=False,timeout=5) as client:
        for path,event in [('http/global',False),('http/event',True),('ui/event',True)]:
            for mode in ('valid','oversize','truncated','unsupported'):
                response=client.get(url+'/runtimes/'+'a'*64+'/'+path+'/'+mode,headers=headers)
                assert response.status_code==(502 if mode=='unsupported' or (not event and mode!='valid') else 200)
                if mode=='valid':assert response.content==(b'data: valid\n\n' if event else b'{"healthy":true}')
                if mode=='oversize' and event:assert response.content==b''
                assert 'Traceback' not in response.text and 'disposable' not in response.text


def test_rebuilt_controller_decoder_allocation_bound(relay_controller):
    name,_,_=relay_controller
    script='''
import asyncio,gzip,tracemalloc,httpx
from runtime_controller.http_decode import decoded_bytes
async def main():
    wire=gzip.compress(b'x'*(16*1024*1024))
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):yield wire
    response=httpx.Response(200,headers={'content-encoding':'gzip'},stream=Stream())
    # Exclude first-use AnyIO cleanup backend/module initialization.
    warm=httpx.Response(200,headers={'content-encoding':'gzip'},stream=httpx.ByteStream(gzip.compress(b'warm')))
    async for _ in decoded_bytes(warm,100):pass
    await warm.aclose()
    tracemalloc.start()
    try:
        async for chunk in decoded_bytes(response,128*1024):pass
    except httpx.DecodingError:pass
    else:raise AssertionError('oversize response accepted')
    _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    assert peak<1024*1024,peak
    await response.aclose()
    print('bounded decoder peak_bytes='+str(peak))
asyncio.run(main())
'''
    result=docker('exec',name,'python','-c',script)
    assert 'bounded decoder peak_bytes=' in result.stdout
