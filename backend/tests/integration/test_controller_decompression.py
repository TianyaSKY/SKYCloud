"""Standalone rebuilt Controller and native HTTP; engine target is a synthetic seam."""
import os
import time
import uuid
import httpx
import pytest
from tests.integration.test_runtime_security import docker

pytestmark=pytest.mark.skipif(os.getenv('RUN_CONTROLLER_DIAGNOSTICS')!='1',reason='Opt-in rebuilt Controller boundary')


@pytest.fixture
def relay_controller(request):
    name='skycloud-controller-decode-'+uuid.uuid4().hex[:12]
    script='''
import gzip,threading,json,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import uvicorn
from runtime_controller import app as controller_app
from runtime_controller.policy import Settings
closed_reads=[]
opened_reads=[]
class Provider(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.headers.get('Accept-Encoding') == 'gzip, deflate, identity'
        event='/event/' in self.path
        mode=self.path.rsplit('/',1)[-1]
        if mode in ('headerquiet','health','trickle'):
            opened_reads.append(self.path)
            self.connection.settimeout(.01)
            if mode=='trickle':self.connection.sendall(b'HTTP/1.1 200 OK\\r\\nX-Slow: ')
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                try:
                    if self.connection.recv(1)==b'':
                        closed_reads.append(self.path);break
                except TimeoutError:pass
                except OSError:
                    closed_reads.append(self.path);break
                if mode=='trickle':
                    try:self.connection.sendall(b'x')
                    except OSError:
                        closed_reads.append(self.path);break
            return
        if mode=='quiet':
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream' if event else 'application/json')
            self.send_header('Content-Length','1')
            self.end_headers();self.wfile.flush()
            self.connection.settimeout(3)
            try:
                if self.connection.recv(1)==b'':closed_reads.append(self.path)
            except OSError:pass
            return
        if mode in ('status','opened'):
            output=json.dumps(closed_reads if mode=='status' else opened_reads).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(output)))
            self.end_headers();self.wfile.write(output);return
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
controller_app.MAX_HEADER_SECONDS=.05
controller_app.MAX_HEALTH_HEADER_SECONDS=.05
controller_app.MAX_RESPONSE=1024
controller_app.MAX_RESPONSE_SECONDS=.05
controller_app.MAX_EVENT_SECONDS=.05
uvicorn.run(controller_app.create_app(settings,Engine()),host='0.0.0.0',port=9101,log_level='warning')
'''
    if getattr(request, 'param', None) == 'disconnect':
        script=script.replace('controller_app.MAX_HEADER_SECONDS=.05','controller_app.MAX_HEADER_SECONDS=60')
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


@pytest.mark.parametrize('path,event',[('http/global',False),('http/event',True),('ui/event',True)])
def test_native_controller_quiet_deadline_disconnects_upstream(relay_controller,path,event):
    _,url,headers=relay_controller
    with httpx.Client(trust_env=False,timeout=2) as client:
        response=client.get(url+'/runtimes/'+'a'*64+'/'+path+'/quiet',headers=headers)
        assert response.status_code==(200 if event else 502)
        if event:assert response.content==b''
        else:assert response.json()=={'detail':'Runtime response timed out'}
        # Independently prove that Controller closed the actual native provider
        # connection; no injected reader cancellation or transport here.
        deadline=time.monotonic()+1
        while True:
            status=client.get(url+'/runtimes/'+'a'*64+'/http/global/status',headers=headers)
            if '/'+path.split('/',1)[1]+'/quiet' in status.json():break
            assert time.monotonic()<deadline,'native upstream did not observe disconnect'
            time.sleep(.01)


@pytest.mark.parametrize('path',['http/session/headerquiet','ui/event/headerquiet','http/global/health','http/session/trickle'])
def test_native_controller_header_deadline_closes_connection(relay_controller,path):
    _,url,headers=relay_controller
    with httpx.Client(trust_env=False,timeout=2) as client:
        response=client.get(url+'/runtimes/'+'a'*64+'/'+path,headers=headers)
        assert response.status_code==504 and response.json()=={'detail':'Runtime upstream timed out'}
        deadline=time.monotonic()+1
        while True:
            status=client.get(url+'/runtimes/'+'a'*64+'/http/global/status',headers=headers)
            if '/'+path.split('/',1)[1] in status.json():break
            assert time.monotonic()<deadline,'header wait retained native upstream'
            time.sleep(.01)


@pytest.mark.parametrize('relay_controller',['disconnect'],indirect=True)
def test_native_controller_caller_disconnect_during_headers(relay_controller):
    import socket
    from urllib.parse import urlparse
    _,url,headers=relay_controller
    location=urlparse(url)
    with socket.create_connection((location.hostname,location.port),timeout=2) as downstream:
        raw=('GET /runtimes/'+'a'*64+'/http/session/headerquiet HTTP/1.1\r\nHost: controller.test\r\nX-Runtime-Controller-Token: '+headers['X-Runtime-Controller-Token']+'\r\n\r\n')
        downstream.sendall(raw.encode())
        # Confirm native upstream admission independently before closing the caller.
        with httpx.Client(trust_env=False,timeout=2) as observer:
            deadline=time.monotonic()+1
            while True:
                opened=observer.get(url+'/runtimes/'+'a'*64+'/http/global/opened',headers=headers)
                if '/session/headerquiet' in opened.json():break
                assert time.monotonic()<deadline,'native header operation did not start'
                time.sleep(.01)
    with httpx.Client(trust_env=False,timeout=2) as client:
        deadline=time.monotonic()+2
        while True:
            status=client.get(url+'/runtimes/'+'a'*64+'/http/global/status',headers=headers)
            if '/session/headerquiet' in status.json():break
            assert time.monotonic()<deadline,'caller disconnect did not release native upstream'
            time.sleep(.02)
