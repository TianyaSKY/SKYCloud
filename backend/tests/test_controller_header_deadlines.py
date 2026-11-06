"""控制器请求头具有总准入截止时间, 独立于读取超时测试."""
import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from runtime_controller import app as controller
from runtime_controller.policy import Settings


@pytest.mark.parametrize('mode', ['quiet', 'late'])
@pytest.mark.parametrize('path', ['http/session', 'ui/event', 'http/global/health'])
async def test_controller_slow_headers_reach_total_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str, mode: str
) -> None:
    monkeypatch.setattr(controller, 'MAX_HEADER_SECONDS', 0.03, raising=False)
    monkeypatch.setattr(controller, 'MAX_HEALTH_HEADER_SECONDS', 0.03, raising=False)
    engine = Mock()
    engine.target.return_value = ('http://runtime.test', ('server', 'secret'))
    settings = Settings('T' * 48, 'S' * 48, 'test/runtime@sha256:' + 'a' * 64, 'test', '1g', 100000, 'test', str(tmp_path))
    stopped: list[str] = []
    late: list[httpx.Response] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
            raise RuntimeError("unreachable")
        except asyncio.CancelledError:
            if mode == 'quiet':
                raise
            await asyncio.sleep(0.01)
            response = httpx.Response(200, stream=httpx.ByteStream(b'private-late'))
            assert not response.is_closed
            late.append(response)
            return response
        finally:
            stopped.append('send')

    actual_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def client(**kwargs: Any) -> httpx.AsyncClient:
        opened = actual_client(transport=httpx.MockTransport(provider), **kwargs)
        clients.append(opened)
        return opened

    monkeypatch.setattr(controller.httpx, 'AsyncClient', client)
    async with actual_client(
        transport=httpx.ASGITransport(app=controller.create_app(settings, engine)),
        base_url='http://controller.test',
    ) as downstream:
        result = await asyncio.wait_for(
            downstream.get(
                '/runtimes/' + 'a' * 64 + '/' + path,
                headers={'X-Runtime-Controller-Token': settings.token},
            ),
            0.3,
        )
    assert result.status_code == 504
    assert result.json() == {'detail': 'Runtime upstream timed out'}
    assert stopped == ['send'] and all(c.is_closed for c in clients)
    assert all(response.is_closed for response in late)
    assert len(late) == (mode == 'late')
