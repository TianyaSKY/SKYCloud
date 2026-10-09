"""Assistant authorization is checked on its SQL worker during quiet waits."""
import asyncio
import threading
import pytest
from app.exceptions import AuthenticationError
from app.features.assistant import service
from app.features.assistant.event_protocol import AssistantEvent
from app.models.assistant import AssistantMessage


@pytest.mark.parametrize('phase', ['initial','event','quiet'])
async def test_credential_loss_cancels_durable_run_and_upstream(session,test_user,test_workspace,monkeypatch,phase):
    conversation=service.create_conversation(session,test_workspace,test_user.id,'fast','test')
    _,run,history=service.prepare_run(session,test_workspace,test_user.id,conversation.id,'question')
    scope=(test_workspace.id,test_user.id,conversation.id,run.id)
    message_id=run.assistant_message_id
    revoked=[phase=='initial']
    calls,received=[],[]
    proceed=asyncio.Event()
    loop_thread=threading.get_ident()
    class Credential:
        def validate(self,db,user_id):
            assert threading.get_ident()!=loop_thread and db is session and user_id==scope[1]
            calls.append('check')
            if revoked[0]:raise AuthenticationError('private-auth-error')
    class Engine:
        async def stream(self,controller,**kwargs):
            async def abort():calls.append('abort')
            controller.set_abort_callback(abort)
            try:
                yield AssistantEvent('token',{'content':'allowed'})
                await proceed.wait()
                if phase=='quiet':await asyncio.Event().wait()
                yield AssistantEvent('token',{'content':'private-late'})
            finally:calls.append('closed')
    monkeypatch.setattr(service,'FastEngine',Engine)
    async def collect():
        async for event in service.stream_run(lambda:session,*scope,history,credential=Credential()):
            received.append(event)
            if event.type=='token':
                revoked[0]=True
                proceed.set()
    await asyncio.wait_for(collect(),3)
    assert received[-1].type=='cancelled'
    assert [e.payload['content'] for e in received if e.type=='token']==([] if phase=='initial' else ['allowed'])
    assert 'private-' not in str([e.payload for e in received])
    if phase!='initial':assert 'abort' in calls and 'closed' in calls
    assert service.run_registry.get(scope[-1]) is None
    # SQL finish occurs before owned close; reload using fixture Session.
    from app.models.assistant import AssistantRun
    assert session.get(AssistantRun,scope[-1]).status=='cancelled'
    assert session.get(AssistantMessage,message_id).status=='cancelled'


async def test_idle_checks_do_not_emit_early_heartbeats_and_drain_model():
    calls,closed=[],[]
    async def source():
        try:
            await asyncio.Event().wait()
            yield AssistantEvent('token',{})
        finally:closed.append(True)
    async def check():
        calls.append(True)
        if len(calls)==3:raise AuthenticationError('revoked')
    emitted=[]
    with pytest.raises(AuthenticationError):
        async for event in service._engine_events_with_heartbeats(source(),1,interval_seconds=15,on_idle=check,poll_seconds=0.01):
            emitted.append(event)
    assert len(calls)==3 and closed==[True] and not emitted


@pytest.mark.parametrize('cause', ['auth','close','cancel','abort-fault','abort-cancel'])
async def test_abort_precedes_provider_close_and_repeated_cancel_is_drained(monkeypatch,cause):
    from contextlib import aclosing
    calls=[]
    entered,aborting,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
    original=AuthenticationError('original authorization error')
    async def source():
        try:
            entered.set()
            if cause=='close':yield AssistantEvent('token',{'content':'allowed'})
            await asyncio.Event().wait()
            yield AssistantEvent('done',{})
        finally:
            assert 'abort' in calls
            calls.append('close')
    async def abort():
        calls.append('abort')
        aborting.set()
        if cause=='cancel':await release.wait()
        if cause=='abort-fault':raise RuntimeError('private-abort-url')
        if cause=='abort-cancel':raise asyncio.CancelledError()
    async def idle():raise original
    logged=[]
    monkeypatch.setattr(service.logger,'error',lambda msg,*args:logged.append(msg.format(*args)))
    async def collect():
        async with aclosing(service._engine_events_with_heartbeats(
            source(),1,on_idle=idle if cause in ('auth','abort-fault','abort-cancel') else None,
            poll_seconds=0.01,on_abort=abort,
        )) as events:
            async for event in events:
                if cause=='close':break
    pending=asyncio.create_task(collect())
    await asyncio.wait_for(entered.wait(),1)
    if cause=='cancel':
        pending.cancel()
        await asyncio.wait_for(aborting.wait(),1)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and calls==['abort']
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    elif cause=='close':await asyncio.wait_for(pending,1)
    else:
        with pytest.raises(AuthenticationError) as error:await asyncio.wait_for(pending,1)
        assert error.value is original
    assert calls==['abort','close']
    assert 'private-' not in str(logged)


@pytest.mark.parametrize('fails',[False,True])
async def test_controller_signals_engine_after_abort_rpc(fails):
    controller=service.RunController()
    calls=[]
    async def engine():
        await controller.cancel_event.wait()
        calls.append('client-close')
    async def abort():
        await asyncio.sleep(0)
        assert not controller.cancel_event.is_set()
        calls.append('abort')
        if fails:raise RuntimeError('transport failure')
    controller.set_abort_callback(abort)
    waiting=asyncio.create_task(engine())
    await controller.request_cancel()
    await waiting
    assert calls==['abort','client-close']
