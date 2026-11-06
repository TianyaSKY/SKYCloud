# pyright: reportPrivateUsage=false
"""取消的文件读取在其 worker 关闭 Session 前不得完成测试."""
import asyncio
import threading
from typing import Any, cast
import pytest
from app.features.file import search_service, service


@pytest.mark.parametrize('kind', ['search', 'status'])
async def test_cancelled_file_read_drains_owned_session(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    entered, release = threading.Event(), threading.Event()
    closed: list[int] = []
    class Session:
        def __enter__(self) -> 'Session':
            return self
        def __exit__(self, *args: Any) -> None:
            closed.append(threading.get_ident())
    def query(*args: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(3)
        return {'items': [], 'total': 0}
    operation: Any
    if kind == 'search':
        monkeypatch.setattr(search_service, '_search_files_fuzzy', query)
        operation = search_service.search_files(None, 1, search_service.SearchFilesParams(query='query'), session_factory=cast(Any, Session))
    else:
        monkeypatch.setattr(service, '_process_status', query)
        operation = service.process_status(None, 1, session_factory=cast(Any, Session))

    pending = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
        await asyncio.sleep(.01)
        pending.cancel()
        await asyncio.sleep(.01)
        assert not pending.done() and not closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        for _ in range(100):
            if closed:
                break
            await asyncio.sleep(.005)
    assert len(closed) == 1
