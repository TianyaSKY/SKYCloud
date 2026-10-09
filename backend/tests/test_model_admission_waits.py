import asyncio
import pytest
from fastapi import HTTPException
from app.infra.http_wait import guarded_wait


@pytest.mark.parametrize('cause',['revoke','timeout','cancel','late-response','abort-fault'])
async def test_wait_drains_operation_and_cleans_completed_late_response(cause):
    entered,release=asyncio.Event(),asyncio.Event()
    calls=[]
    async def operation():
        try:
            entered.set()
            if cause not in ('late-response','abort-fault'):await asyncio.Event().wait()
            return 'opened'
        finally:calls.append('operation-close')
    async def check():
        if cause in ('revoke','late-response','abort-fault'):raise HTTPException(401,'revoked')
    async def abort():
        calls.append('abort')
        if cause=='abort-fault':raise RuntimeError('private-cleanup-url')
        if cause=='cancel':await release.wait()
    pending=asyncio.create_task(guarded_wait(operation(),check=check,seconds=0.03,status=504,
        detail='timeout',interval=0.01,on_abort=abort))
    await entered.wait()
    if cause=='cancel':
        pending.cancel();await asyncio.sleep(0)
        pending.cancel();await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    else:
        with pytest.raises(HTTPException) as error:await asyncio.wait_for(pending,1)
        assert error.value.status_code==(504 if cause=='timeout' else 401)
    assert calls.count('abort')==1 and calls.count('operation-close')==1


async def test_successful_wait_transfers_resource_without_abort():
    calls=[]
    async def operation():return 'opened'
    async def check():calls.append('check')
    async def abort():pytest.fail('successful wait must retain upstream')
    assert await guarded_wait(operation(),check=check,seconds=1,status=504,detail='timeout',on_abort=abort)=='opened'
    assert calls==['check']
