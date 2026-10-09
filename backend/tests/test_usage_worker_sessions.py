"""Token usage route SQL and response preparation run on an owning worker."""
import asyncio
import threading
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI
from app.api.dependencies import get_current_user, require_admin
from app.exceptions import ResourceNotFoundError, register_exception_handlers
from app.features.token_usage import router
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('path,boundary,target', [
    ('/token-usage/stats', 'get_user_token_stats', 1),
    ('/token-usage/stats/2', 'get_user_token_stats', 2),
    ('/token-usage/logs', 'get_usage_logs', 1),
    ('/token-usage/logs/2', 'get_usage_logs', 2),
    ('/token-usage/daily', 'get_daily_stats', 1),
    ('/token-usage/daily/2', 'get_daily_stats', 2),
    ('/admin/token-usage/users', 'get_all_users_token_stats', None),
    ('/admin/token-usage/logs', 'get_all_users_usage_logs', None),
    ('/admin/token-usage/daily', 'get_all_users_daily_stats', None),
    ('/admin/token-usage/daily/per-user', 'get_per_user_daily_stats', None),
])
async def test_usage_worker_owner_cleanup_and_current_role_gate(monkeypatch, path, boundary, target):
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
    def authorize(session, actor_id, user_id=None):
        assert threading.get_ident() == session.thread
        assert actor_id == 1 and user_id == target
        authorized.append(session)
        return 'admin'
    def missing(session, *args, **kwargs):
        assert threading.get_ident() == session.thread
        assert authorized == [session]
        entered.set(); assert release.wait(5)
        raise ResourceNotFoundError('Not found')
    monkeypatch.setattr(router.token_usage_service, boundary, missing)
    monkeypatch.setattr(router.user_service, "authorize_account_mutation", authorize)
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, role="admin")
    app.dependency_overrides[get_session_factory] = lambda: Owned
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.get(path))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 404
    assert len(opened) == 1 and closed == opened
