"""Context restoration requires acknowledged persistence before model dispatch."""
import asyncio
import httpx
import pytest
from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeClientError


def test_context_persists_synchronously_and_normal_prompt_remains_async():
    calls=[]
    def handle(request):
        import json
        calls.append((request.url.path,json.loads(request.content)))
        if request.url.path.endswith('/message'):
            return httpx.Response(200,json={'info':{'id':'message','sessionID':'session','role':'user'},'parts':[]})
        return httpx.Response(204)
    async def check():
        async with OpenCodeClient('http://runtime','user','password') as client:
            await client._client.aclose()
            client._client=httpx.AsyncClient(base_url='http://runtime',transport=httpx.MockTransport(handle))
            await client.prompt_async('session','history','expert',no_reply=True,system='system')
            await client.prompt_async('session','question','expert')
    asyncio.run(check())
    assert [path for path,_ in calls]==['/session/session/message','/session/session/prompt_async']
    assert calls[0][1]['noReply'] is True and calls[0][1]['system']=='system'
    assert calls[1][1]['noReply'] is False


@pytest.mark.parametrize('body',[None,{}, {'info':{'id':'message','sessionID':'other','role':'user'}}, {'info':{'id':'message','sessionID':'session','role':'assistant'}}])
def test_unconfirmed_context_persistence_is_rejected(body):
    async def check():
        async with OpenCodeClient('http://runtime','user','password') as client:
            await client._client.aclose()
            client._client=httpx.AsyncClient(base_url='http://runtime',transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body)))
            with pytest.raises(OpenCodeClientError,match='confirm persisted context'):
                await client.prompt_async('session','history','expert',no_reply=True)
    asyncio.run(check())
