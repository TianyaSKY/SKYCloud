# pyright: reportPrivateUsage=false
import asyncio
from collections.abc import AsyncIterator
import threading
from typing import Any, cast
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.features.runtime_llm import router


@pytest.mark.parametrize('cause', ['revoked', 'deadline'])
async def test_quiet_model_wait_rechecks_and_closes_pending_read(monkeypatch: pytest.MonkeyPatch, cause: str) -> None:
    entered, closed = asyncio.Event(), asyncio.Event()
    checks: list[bool] = []
    loop_thread = threading.get_ident()
    class Response:
        headers: dict[str, str] = {}
        async def aiter_bytes(self) -> AsyncIterator[bytes]:
            try:
                entered.set()
                await asyncio.Event().wait()
                yield b'never'
            finally:
                closed.set()
    def validate(request: Any, factory: Any, **kwargs: Any) -> None:
        assert threading.get_ident() != loop_thread
        checks.append(True)
        if cause == 'revoked':
            raise HTTPException(401, 'revoked')
    monkeypatch.setattr(router, '_validate_current_credential', validate)
    monkeypatch.setattr(router, 'RECHECK_SECONDS', 0.01)
    monkeypatch.setattr(router, 'MAX_STREAM_SECONDS', 0.03)
    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}
    request = Request({'type': 'http', 'headers': []}, receive)
    async def collect() -> list[bytes]:
        return [part async for part in router.authorized_bytes(cast(Any, Response()), request, cast(Any, None))]
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(collect(), 1)
    assert error.value.status_code == (401 if cause == 'revoked' else 502)
    assert checks and entered.is_set() and closed.is_set()


async def test_disconnected_model_client_closes_pending_reader_without_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = asyncio.Event()
    class Response:
        headers: dict[str, str] = {}
        async def aiter_bytes(self) -> AsyncIterator[bytes]:
            try:
                await asyncio.Event().wait()
                yield b'never'
            finally:
                closed.set()
    async def receive() -> dict[str, Any]:
        return {'type': 'http.disconnect'}
    request = Request({'type': 'http', 'headers': []}, receive)
    def validate(*args: Any, **kwargs: Any) -> None:
        pytest.fail('disconnected client must stop before SQL')
    monkeypatch.setattr(router, '_validate_current_credential', validate)
    monkeypatch.setattr(router, 'RECHECK_SECONDS', 0.01)
    async def collect() -> list[bytes]:
        return [part async for part in router.authorized_bytes(cast(Any, Response()), request, cast(Any, None))]
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(collect(), 1)
    assert error.value.status_code == 499 and closed.is_set()


@pytest.mark.parametrize('cause', ['revoked', 'disconnect', 'deadline', 'cancel', 'eof'])
@pytest.mark.parametrize('close_error', [RuntimeError, asyncio.CancelledError])
async def test_model_iterator_cleanup_fault_preserves_outcome(monkeypatch: pytest.MonkeyPatch, cause: str, close_error: Any) -> None:
    started = asyncio.Event()
    calls: list[str] = []
    logs: list[str] = []
    import runtime_common.http_stream as shared_stream
    def log_error(message: str, *args: Any) -> None:
        logs.append(message % args)
    monkeypatch.setattr(shared_stream.logger, 'error', log_error)
    class Iterator:
        def __aiter__(self) -> 'Iterator':
            return self
        async def __anext__(self) -> bytes:
            try:
                started.set()
                if cause == 'eof':
                    raise StopAsyncIteration
                await asyncio.Event().wait()
                return b''
            finally:
                calls.append('read-finished')
        async def aclose(self) -> None:
            calls.append('close')
            raise close_error('private-provider-credential')
    class Response:
        headers: dict[str, str] = {}
        def aiter_bytes(self) -> Iterator:
            return Iterator()
    async def verify(*args: Any) -> None:
        if cause in ('revoked', 'disconnect'):
            raise HTTPException(401 if cause == 'revoked' else 499, 'fixed')
    monkeypatch.setattr(router, 'verify_connected', verify)
    monkeypatch.setattr(router, 'RECHECK_SECONDS', 0.01)
    monkeypatch.setattr(router, 'MAX_STREAM_SECONDS', 0.03)
    async def collect() -> list[bytes]:
        return [part async for part in router.authorized_bytes(cast(Any, Response()), cast(Any, None), cast(Any, None))]
    pending = asyncio.create_task(collect())
    await started.wait()
    if cause == 'cancel':
        pending.cancel()
        with pytest.raises(asyncio.CancelledError) as cancel_error:
            await asyncio.wait_for(pending, 1)
        assert 'private-provider-credential' not in str(cancel_error.value)
    elif cause == 'eof':
        assert await asyncio.wait_for(pending, 1) == []
    else:
        with pytest.raises(HTTPException) as http_error:
            await asyncio.wait_for(pending, 1)
        assert http_error.value.status_code == {'revoked': 401, 'disconnect': 499, 'deadline': 502}[cause]
    assert calls == ['read-finished', 'close']
    assert logs == [f'Stream iterator close failed (error_type={close_error.__name__})']


async def test_model_repeated_cancel_drains_reader_before_iterator_close(monkeypatch: pytest.MonkeyPatch) -> None:
    started, cleaning, release, closed = (asyncio.Event() for _ in range(4))
    calls: list[str] = []
    class Iterator:
        def __aiter__(self) -> 'Iterator':
            return self
        async def __anext__(self) -> bytes:
            try:
                started.set()
                await asyncio.Event().wait()
                return b''
            finally:
                cleaning.set()
                await release.wait()
                closed.set()
                calls.append('read-finished')
        async def aclose(self) -> None:
            assert closed.is_set()
            calls.append('close')
    class Response:
        headers: dict[str, str] = {}
        def aiter_bytes(self) -> Iterator:
            return Iterator()
    async def verify(*args: Any) -> None:
        pass
    monkeypatch.setattr(router, 'verify_connected', verify)
    async def collect() -> list[bytes]:
        return [part async for part in router.authorized_bytes(cast(Any, Response()), cast(Any, None), cast(Any, None))]
    pending = asyncio.create_task(collect())
    try:
        await started.wait()
        pending.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done(), 'repeated cancellation interrupted reader cleanup'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
    assert calls == ['read-finished', 'close']


async def test_model_cancel_during_successful_eof_cleanup_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    closing, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    class Iterator:
        def __aiter__(self) -> 'Iterator':
            return self
        async def __anext__(self) -> bytes:
            raise StopAsyncIteration
        async def aclose(self) -> None:
            closing.set()
            await release.wait()
            calls.append('close')
    class Response:
        headers: dict[str, str] = {}
        def aiter_bytes(self) -> Iterator:
            return Iterator()
    async def verify(*args: Any) -> None:
        pass
    monkeypatch.setattr(router, 'verify_connected', verify)
    async def collect() -> list[bytes]:
        return [part async for part in router.authorized_bytes(cast(Any, Response()), cast(Any, None), cast(Any, None))]
    pending = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(closing.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
    assert calls == ['close']


async def test_model_anyio_scope_cancel_drains_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    calls: list[str] = []
    scopes: list[anyio.CancelScope] = []
    class Iterator:
        def __aiter__(self) -> 'Iterator':
            return self
        async def __anext__(self) -> bytes:
            try:
                started.set()
                await asyncio.Event().wait()
                return b''
            finally:
                cleaning.set()
                await release.wait()
                calls.append('read-finished')
        async def aclose(self) -> None:
            calls.append('close')
    class Response:
        headers: dict[str, str] = {}
        def aiter_bytes(self) -> Iterator:
            return Iterator()
    async def verify(*args: Any) -> None:
        pass
    monkeypatch.setattr(router, 'verify_connected', verify)
    async def collect() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async for _ in router.authorized_bytes(cast(Any, Response()), cast(Any, None), cast(Any, None)):
                pass
        assert scope.cancelled_caught
    pending = asyncio.create_task(collect())
    try:
        await started.wait()
        scopes[0].cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        await asyncio.sleep(0.01)
        assert not pending.done()
    finally:
        release.set()
        await asyncio.wait_for(pending, 1)
    assert calls == ['read-finished', 'close']
