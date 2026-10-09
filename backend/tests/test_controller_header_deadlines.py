"""Controller headers have a total admission deadline, independent of read timeout."""
import asyncio
from unittest.mock import Mock
import httpx
import pytest
from runtime_controller import app as controller
from runtime_controller.policy import Settings


@pytest.mark.parametrize('mode',['quiet','late'])
@pytest.mark.parametrize('path',['http/session','ui/event','http/global/health'])
async def test_controller_slow_headers_reach_total_deadline(monkeypatch,tmp_path,path,mode):
    monkeypatch.setattr(controller,'MAX_HEADER_SECONDS',.03,raising=False)
    monkeypatch.setattr(controller,'MAX_HEALTH_HEADER_SECONDS',.03,raising=False)
    engine=Mock();engine.target.return_value=('http://runtime.test',('server','secret'))
    settings=Settings('T'*48,'S'*48,'test/runtime@sha256:'+'a'*64,'test','1g',100000,'test',str(tmp_path))
    stopped=[]
    late=[]
    async def provider(request):
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            if mode=='quiet':raise
            await asyncio.sleep(.01)
            response=httpx.Response(200,stream=httpx.ByteStream(b'private-late'))
            assert not response.is_closed
            late.append(response);return response
        finally:stopped.append('send')
    actual_client=httpx.AsyncClient
    clients=[]
    def client(**kwargs):
        opened=actual_client(transport=httpx.MockTransport(provider),**kwargs)
        clients.append(opened);return opened
    monkeypatch.setattr(controller.httpx,'AsyncClient',client)
    async with actual_client(transport=httpx.ASGITransport(app=controller.create_app(settings,engine)),base_url='http://controller.test') as downstream:
        result=await asyncio.wait_for(downstream.get('/runtimes/'+'a'*64+'/'+path,
                      headers={'X-Runtime-Controller-Token':settings.token}),.3)
    assert result.status_code==504
    assert result.json()=={'detail':'Runtime upstream timed out'}
    assert stopped==['send'] and all(client.is_closed for client in clients)
    assert all(response.is_closed for response in late)
    assert len(late)==(mode=='late')
