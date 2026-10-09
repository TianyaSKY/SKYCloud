"""Assistant route SQL and response preparation run on an owning worker."""
import asyncio
import threading
from types import SimpleNamespace
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
async def test_assistant_worker_session_cleanup_and_responsiveness(monkeypatch, method, path, body, boundary):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened, closed = [], []
    class Owned:
        def __init__(self):
            self.thread = threading.get_ident()
            assert self.thread != loop_thread
            opened.append(self)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            assert threading.get_ident() == self.thread
            closed.append(self)
    def missing(session, *args, **kwargs):
        assert threading.get_ident() == session.thread
        entered.set(); assert release.wait(5)
        raise ResourceNotFoundError('Not found')
    monkeypatch.setattr(repository if boundary == "get_owned_run" else router.service, boundary, missing)
    monkeypatch.setattr(router, "assert_member", lambda *args: None)
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
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
