"""Owned Redis admission and token-checked release for expert runs."""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager

from loguru import logger

from app.exceptions import ConflictError, ServiceOperationError
from app.infra import extensions
from app.infra.owned_work import run_owned
from app.infra.async_stream import drain_cleanup


_LOCAL_RUNTIME_LOCKS: dict[int, asyncio.Lock] = {}
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


@asynccontextmanager
async def runtime_lease(runtime_id: int, *, on_lost=None, before_acquire=None):
    local_lock = _LOCAL_RUNTIME_LOCKS.setdefault(runtime_id, asyncio.Lock())
    if local_lock.locked():
        raise ConflictError('Expert runtime is already busy')
    await local_lock.acquire()
    redis_key = f'assistant:expert:runtime:{runtime_id}'
    token = uuid.uuid4().hex
    attempted = False
    failed = False
    lost = False
    keeper = None
    owner = asyncio.current_task()
    loop = asyncio.get_running_loop()
    ttl = None

    def acquire():
        nonlocal attempted
        attempted = True
        deadline = loop.time() + ttl
        result = extensions.redis_client.set(
            redis_key, token, nx=True,
            ex=ttl,
        )
        return result, deadline

    def renew():
        deadline = loop.time() + ttl
        result = extensions.redis_client.eval(_RENEW, 1, redis_key, token, ttl * 1000)
        return result, deadline

    def lose_ownership():
        nonlocal lost
        lost = True
        owner.cancel('expert-runtime-lease-lost')

    async def keep_alive(deadline):
        pending = None
        try:
            while True:
                await asyncio.sleep(min(ttl / 3, max(0, deadline - loop.time())))
                remaining = deadline - loop.time()
                if remaining <= 0:
                    lose_ownership()
                    return
                pending = asyncio.create_task(run_owned(renew))
                ready, _ = await asyncio.wait({pending}, timeout=remaining)
                if not ready:
                    # Signal loss before draining a slow Redis operation. A
                    # delayed reply cannot keep the expert task executing.
                    lose_ownership()
                    return
                result, next_deadline = pending.result()
                pending = None
                if not result or loop.time() >= deadline:
                    lose_ownership()
                    return
                deadline = next_deadline
        except Exception as exc:
            logger.error('Expert runtime lease renewal failed (error_type={})', type(exc).__name__)
            lose_ownership()
        finally:
            if pending is not None:
                pending.cancel()
                async def drain():
                    await asyncio.gather(pending, return_exceptions=True)
                await drain_cleanup(drain())

    def release():
        try:
            # Also clean up an ambiguous SET reply: it may have succeeded on
            # Redis even if the client failed before receiving its response.
            extensions.redis_client.eval(_RELEASE, 1, redis_key, token)
        except Exception as exc:
            logger.error('Expert runtime lease release failed (error_type={})', type(exc).__name__)

    try:
        if before_acquire is not None:
            await before_acquire(token)
        try:
            ttl = int(os.getenv('ASSISTANT_RUNTIME_LOCK_TTL', '3600'))
            if ttl <= 0:
                raise ValueError('Lease lifetime must be positive')
            result, deadline = await run_owned(acquire)
        except Exception as exc:
            logger.error('Expert runtime lease acquisition failed (error_type={})', type(exc).__name__)
            raise ServiceOperationError('Expert runtime lock is temporarily unavailable') from None
        if result is False or result is None:
            raise ConflictError('Expert runtime is already busy')
        if loop.time() >= deadline:
            raise ServiceOperationError('Expert runtime lock confirmation expired')
        keeper = asyncio.create_task(keep_alive(deadline))
        yield token
    except asyncio.CancelledError:
        failed = True
        if lost:
            raise ServiceOperationError('Expert runtime lock ownership was lost') from None
        raise
    except BaseException:
        failed = True
        raise
    finally:
        async def cleanup():
            try:
                if lost and on_lost is not None:
                    try:
                        async with asyncio.timeout(6):
                            await on_lost()
                    except (Exception, asyncio.CancelledError) as exc:
                        logger.error('Expert runtime lease abort failed (error_type={})', type(exc).__name__)
                if keeper is not None:
                    keeper.cancel()
                    await asyncio.gather(keeper, return_exceptions=True)
                if attempted:
                    await run_owned(release, cancel_queued=False)
            finally:
                local_lock.release()
        cancelled = await drain_cleanup(cleanup())
        if cancelled and not failed:
            raise asyncio.CancelledError()
