"""Auxiliary route SQL and response preparation run on an owning worker."""
import asyncio
import threading
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI
from app.api.dependencies import get_current_user, require_admin
from app.exceptions import ResourceNotFoundError, register_exception_handlers
from app.features.inbox import router as inbox
from app.features.sys_dict import router as settings
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('module,method,path,body,boundary', [
    (inbox, 'GET', '/inbox', None, 'get_user_inbox'),
    (inbox, 'PUT', '/inbox/2/read', None, 'mark_as_read'),
    (inbox, 'PUT', '/inbox/read-all', None, 'mark_all_as_read'),
    (inbox, 'DELETE', '/inbox/2', None, 'delete_inbox_message'),
    (settings, 'POST', '/sys_dicts', {'key':'k','value':'v'}, 'create_sys_dict'),
    (settings, 'GET', '/sys_dicts/2', None, 'get_sys_dict_sync'),
    (settings, 'PUT', '/sys_dicts/2', {'key':'k','value':'v'}, 'update_sys_dict'),
    (settings, 'DELETE', '/sys_dicts/2', None, 'delete_sys_dict'),
    (settings, 'GET', '/sys_dicts', None, 'get_sys_dict_all_sync'),
])
async def test_aux_session_owner_and_denied_cleanup(monkeypatch, module, method, path, body, boundary):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened, closed, authorized = [], [], []
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
    def authorize(session, actor_id):
        assert actor_id == 1 and threading.get_ident() == session.thread
        authorized.append(session)
        return 'admin'
    def missing(session, *args):
        assert threading.get_ident() == session.thread
        if module is settings: assert authorized == [session]
        entered.set(); assert release.wait(5)
        raise ResourceNotFoundError('Not found')
    service = inbox.inbox_service if module is inbox else settings.sys_dict_service
    monkeypatch.setattr(service, boundary, missing)
    monkeypatch.setattr(settings.user_service, "authorize_account_mutation", authorize)
    app = FastAPI(); register_exception_handlers(app); app.include_router(module.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, role="admin")
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
