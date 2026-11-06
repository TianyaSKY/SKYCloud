"""助手路由 SQL 与响应准备在所属 worker 上运行测试."""
import asyncio
import threading
from types import SimpleNamespace
from typing import Any
import httpx
import pytest
from fastapi import FastAPI
from app.api.dependencies import get_current_user, get_current_workspace
from app.exceptions import ResourceNotFoundError, register_exception_handlers
from app.features.assistant import router, repository
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('method,path,body,boundary', [
    ('POST','/assistant/conversations',{'mode':'fast'},'create_conversation'),
    ('GET','/assistant/conversations',None,'list_user_conversations'),
    ('PATCH','/assistant/conversations/2',{'title':'new'},'update_conversation'),
    ('DELETE','/assistant/conversations/2',None,'delete_conversation'),
    ('GET','/assistant/conversations/2/messages',None,'get_user_conversation'),
    ('GET','/assistant/conversations/2/active-run',None,'get_active_run_for_conversation'),
    ('POST','/assistant/conversations/2/messages/stream',{'query':'hello'},'get_user_conversation'),
    ('POST','/assistant/runs/2/cancel',None,'request_cancel'),
    ('GET','/assistant/runs/active',None,'get_active_expert_run_for_member'),
    ('GET','/assistant/runs/2',None,'get_owned_run'),
    ('POST','/assistant/runs/2/permissions/p1',{'response':'once'},'respond_permission'),
    ('POST','/assistant/conversations/2/handoff',{},'handoff_to_expert'),
])
async def test_assistant_worker_session_cleanup_and_responsiveness(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    boundary: str,
) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened: list["Owned"] = []
    closed: list["Owned"] = []

    class Owned:
        def __init__(self) -> None:
            self.thread = threading.get_ident()
            assert self.thread != loop_thread
            opened.append(self)

        def __enter__(self) -> "Owned":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

        def close(self) -> None:
            assert threading.get_ident() == self.thread
            closed.append(self)

    def missing(session: Owned, *args: object, **kwargs: object) -> None:
        assert threading.get_ident() == session.thread
        entered.set()
        assert release.wait(5)
        raise ResourceNotFoundError('Not found')

    def noop_assert_member(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(repository if boundary == "get_owned_run" else router.service, boundary, missing)
    monkeypatch.setattr(router, "assert_member", noop_assert_member)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1, http_stream_credential=None,
    )
    app.dependency_overrides[get_current_workspace] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: Owned
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.request(method, path, json=body))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 404
    assert len(opened) == 1 and closed == opened
