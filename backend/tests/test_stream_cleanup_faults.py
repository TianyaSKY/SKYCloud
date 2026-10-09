"""Stream cleanup preserves outcomes and stays on the owner thread."""
import asyncio
import threading
from types import SimpleNamespace
import pytest
from app.exceptions import ResourceNotFoundError
from app.features.assistant import service, stream_persistence
from app.features.assistant.event_protocol import AssistantEvent


@pytest.mark.parametrize('borrowed', [False, True])
@pytest.mark.parametrize('outcome', ['done','business-error','creation-error','cancel'])
@pytest.mark.parametrize('invalidation_fails', [False, True])
async def test_cleanup_fault_preserves_stream_outcome_and_thread(monkeypatch, borrowed, outcome, invalidation_fails):
    calls, logged = [], []
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    original = ResourceNotFoundError('original business failure')
    class Owned:
        def __init__(self): self.owner = None
        def check(self):
            if self.owner is None: self.owner = threading.get_ident()
            assert self.owner != loop_thread and threading.get_ident() == self.owner
        def get(self, *args): self.check(); return SimpleNamespace()
        def close(self): self.fail('close')
        def rollback(self): self.fail('rollback')
        def fail(self, phase):
            self.check(); calls.append(phase)
            raise RuntimeError('private-connection-url')
        def invalidate(self):
            self.check(); calls.append('invalidate')
            if invalidation_fails: raise RuntimeError('private-invalidation-url')
    session = Owned()
    def persistence(owned, *args):
        assert owned is session
        owned.check()
        if outcome == 'creation-error': raise original
        return owned
    async def events(worker, owned, history):
        if outcome == 'business-error': raise original
        if outcome == 'cancel':
            entered.set()
            await asyncio.Event().wait()
        yield AssistantEvent('done', {})
    monkeypatch.setattr(stream_persistence, 'StreamPersistence', persistence)
    monkeypatch.setattr(service, '_stream_persisted', events)
    monkeypatch.setattr(service.logger, 'error', lambda message,*args: logged.append(message.format(*args)))
    iterator = (service.stream_prepared_run(session, None, 1, None, None, []) if borrowed
                else service.stream_run(lambda: session, 1, 1, 1, 1, []))
    async def collect(): return [event.type async for event in iterator]
    if outcome == 'done': assert await collect() == ['done']
    elif outcome == 'cancel':
        task = asyncio.create_task(collect())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        with pytest.raises(ResourceNotFoundError) as error: await collect()
        assert error.value is original
    assert calls == [('rollback' if borrowed else 'close'), 'invalidate']
    assert logged and 'private-' not in str(logged)
