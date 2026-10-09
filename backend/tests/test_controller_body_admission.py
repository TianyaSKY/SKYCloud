"""A bounded body has both idle and total ingestion deadlines before dispatch."""
import asyncio
import json
import pytest
from runtime_controller import app as controller


@pytest.mark.parametrize('mode',['drip','idle','late-final'])
async def test_body_total_or_idle_deadline_prevents_dispatch(monkeypatch,mode):
    monkeypatch.setattr(controller,'MAX_BODY_SECONDS',.03,raising=False)
    monkeypatch.setattr(controller,'MAX_BODY_IDLE_SECONDS',.01 if mode=='idle' else .02,raising=False)
    now=[0]
    if mode=='late-final':
        monkeypatch.setattr(controller,'monotonic',lambda:now[0],raising=False)
    sent,dispatched=[],[]
    received=0
    async def target(*args):dispatched.append(True)
    async def receive():
        nonlocal received
        if mode=='idle':
            await asyncio.sleep(.05)
            return {'type':'http.request','body':b'late','more_body':False}
        if mode=='late-final':
            now[0]=1
            return {'type':'http.request','body':b'late','more_body':False}
        await asyncio.sleep(.01)
        received+=1
        return {'type':'http.request','body':b'x','more_body':received<5}
    async def send(message):sent.append(message)
    scope={'type':'http','headers':[(b'x-runtime-controller-token',b'T'*48)]}
    await asyncio.wait_for(controller.AuthAndBodyLimit(target,'T'*48)(scope,receive,send),.2)
    assert not dispatched
    assert sent[0]['status']==408
    assert json.loads(sent[1]['body'])=={'detail':'Controller request timed out'}


async def test_fragmented_body_replayed_once_without_consuming_disconnect():
    messages=[{'type':'http.request','body':b'a','more_body':True},
              {'type':'http.request','body':b'b','more_body':False},{'type':'http.disconnect'}]
    seen=[]
    async def receive():return messages.pop(0)
    async def target(scope,receive,send):
        seen.append(await receive());seen.append(await receive())
    async def send(message):pytest.fail('middleware must dispatch valid input')
    await controller.AuthAndBodyLimit(target,'T'*48)({'type':'http','headers':[(b'x-runtime-controller-token',b'T'*48)]},receive,send)
    assert seen==[{'type':'http.request','body':b'ab','more_body':False},{'type':'http.disconnect'}]
    assert not messages


async def test_body_cancellation_drains_receive_before_exit():
    entered,closing,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
    calls=[]
    async def receive():
        entered.set()
        try:await asyncio.Event().wait()
        finally:closing.set();await release.wait();calls.append('drained')
    async def target(*args):pytest.fail('cancelled body must not dispatch')
    async def send(message):pytest.fail('cancelled caller must not receive a timeout response')
    operation=controller.AuthAndBodyLimit(target,'T'*48)({'type':'http','headers':[(b'x-runtime-controller-token',b'T'*48)]},receive,send)
    pending=asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(),1)
        pending.cancel()
        await asyncio.wait_for(closing.wait(),1)
        pending.cancel();await asyncio.sleep(.01)
        assert not pending.done() and not calls
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    assert calls==['drained']
