import asyncio
import threading
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.features.runtime_llm import router


@pytest.mark.parametrize('cause',['revoked','deadline'])
async def test_quiet_model_wait_rechecks_and_closes_pending_read(monkeypatch,cause):
    entered,closed=asyncio.Event(),asyncio.Event()
    checks=[]
    loop_thread=threading.get_ident()
    class Response:
        async def aiter_bytes(self):
            try:
                entered.set();await asyncio.Event().wait();yield b'never'
            finally:closed.set()
    def validate(request,factory,**kwargs):
        assert threading.get_ident()!=loop_thread
        checks.append(True)
        if cause=='revoked':raise HTTPException(401,'revoked')
    monkeypatch.setattr(router,'_validate_current_credential',validate)
    monkeypatch.setattr(router,'RECHECK_SECONDS',0.01)
    monkeypatch.setattr(router,'MAX_STREAM_SECONDS',0.03)
    async def receive():await asyncio.Event().wait()
    request=Request({'type':'http','headers':[]},receive)
    async def collect():return [part async for part in router.authorized_bytes(Response(),request,None)]
    with pytest.raises(HTTPException) as error:await asyncio.wait_for(collect(),1)
    assert error.value.status_code==(401 if cause=='revoked' else 502)
    assert checks and entered.is_set() and closed.is_set()


async def test_disconnected_model_client_closes_pending_reader_without_sql(monkeypatch):
    closed=asyncio.Event()
    class Response:
        async def aiter_bytes(self):
            try:await asyncio.Event().wait();yield b'never'
            finally:closed.set()
    async def receive():return {'type':'http.disconnect'}
    request=Request({'type':'http','headers':[]},receive)
    def validate(*args,**kwargs):pytest.fail('disconnected client must stop before SQL')
    monkeypatch.setattr(router,'_validate_current_credential',validate)
    monkeypatch.setattr(router,'RECHECK_SECONDS',0.01)
    async def collect():return [part async for part in router.authorized_bytes(Response(),request,None)]
    with pytest.raises(HTTPException) as error:await asyncio.wait_for(collect(),1)
    assert error.value.status_code==499 and closed.is_set()
