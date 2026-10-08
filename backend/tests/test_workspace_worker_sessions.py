"""All workspace route workers own their business SQL session."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.dependencies import get_current_user
from app.exceptions import PermissionDeniedError, register_exception_handlers
from app.features.workspace import router as workspace
from app.features.workspace import ui_tickets
from app.features.auth.service import generate_token
from app.infra.extensions import get_session_factory


CASES = [
    ('POST', '/workspace', {'name': 'Created'}, 'create_workspace'),
    ('GET', '/workspace', None, 'get_user_workspace_rows'),
    ('GET', '/workspace/2', None, 'assert_member'),
    ('POST', '/workspace/2/opencode/ui-ticket', None, 'issue'),
    *[('POST', '/workspace/2/opencode/' + action, None, 'assert_can_write')
      for action in ('start', 'stop', 'restart', 'setup-mcp', 'runtime/start', 'runtime/stop', 'runtime/restart')],
    ('GET', '/workspace/2/opencode/runtime', None, 'assert_member'),
    ('PUT', '/workspace/2', {'name': 'Updated'}, 'update_workspace'),
    ('DELETE', '/workspace/2', None, 'delete_workspace'),
    ('POST', '/workspace/2/members', {'username': 'member', 'role': 'editor'}, 'invite_member'),
    ('GET', '/workspace/2/members', None, 'assert_member'),
    ('PUT', '/workspace/2/members/3', {'role': 'viewer'}, 'update_member_role'),
    ('DELETE', '/workspace/2/members/3', None, 'remove_member'),
]


@pytest.mark.parametrize('method,path,body,boundary', CASES)
async def test_workspace_route_worker_session_cleanup_and_responsiveness(monkeypatch, method, path, body, boundary):
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

    def denied(session, *args, **kwargs):
        assert threading.get_ident() == session.thread
        entered.set()
        assert release.wait(5)
        raise PermissionDeniedError('Permission denied')

    owner = ui_tickets if boundary == 'issue' else workspace if boundary.startswith('assert_') else workspace.workspace_service
    monkeypatch.setattr(owner, boundary, denied)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(workspace.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: Owned
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.request(method, path, json=body,
            headers={'Authorization': 'Bearer ' + generate_token(1, 0)}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 403
    assert len(opened) == 1 and closed == opened
