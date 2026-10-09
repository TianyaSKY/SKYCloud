"""Owned Redis admission and token-checked release for expert runs."""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager

from loguru import logger

from app.exceptions import ConflictError, ServiceOperationError
from app.infra import extensions
from app.infra.owned_work import run_owned


_LOCAL_RUNTIME_LOCKS: dict[int, asyncio.Lock] = {}
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


@asynccontextmanager
async def runtime_lease(runtime_id: int):
    local_lock = _LOCAL_RUNTIME_LOCKS.setdefault(runtime_id, asyncio.Lock())
    if local_lock.locked():
        raise ConflictError('Expert runtime is already busy')
    await local_lock.acquire()
    redis_key = f'assistant:expert:runtime:{runtime_id}'
    token = uuid.uuid4().hex
    attempted = False
    failed = False

    def acquire():
        nonlocal attempted
        attempted = True
        return extensions.redis_client.set(
            redis_key, token, nx=True,
            ex=int(os.getenv('ASSISTANT_RUNTIME_LOCK_TTL', '3600')),
        )

    def release():
        try:
            # Also clean up an ambiguous SET reply: it may have succeeded on
            # Redis even if the client failed before receiving its response.
            extensions.redis_client.eval(_RELEASE, 1, redis_key, token)
        except Exception as exc:
            logger.error('Expert runtime lease release failed (error_type={})', type(exc).__name__)

    try:
        try:
            result = await run_owned(acquire)
        except Exception as exc:
            logger.error('Expert runtime lease acquisition failed (error_type={})', type(exc).__name__)
            raise ServiceOperationError('Expert runtime lock is temporarily unavailable') from None
        if result is False or result is None:
            raise ConflictError('Expert runtime is already busy')
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            if attempted:
                try:
                    await run_owned(release, cancel_queued=False)
                except asyncio.CancelledError:
                    if not failed:
                        raise
        finally:
            local_lock.release()
