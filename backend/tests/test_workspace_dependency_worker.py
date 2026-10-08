"""Workspace context dependencies create/query/close on their executing worker."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from app.api import dependencies
from app.features.workspace import permissions
from app.infra.extensions import get_session_factory
from app.models.workspace import Workspace


@pytest.mark.parametrize('kind', ['context', 'role'])
async def test_workspace_dependency_worker_ownership(monkeypatch, kind):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened, closed = [], []
    snapshot = Workspace(id=2, name='Owned', owner_id=1)

    class Owned:
        def __init__(self):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            opened.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            assert threading.get_ident() == self.owner
            closed.append(self)

        def get(self, model, id):
            assert threading.get_ident() == self.owner
            assert model is Workspace and id == 2
            if kind == 'context':
                entered.set(); assert release.wait(5)
            return snapshot

    def member(db, *args):
        assert threading.get_ident() == db.owner
        return 'editor'

    def role(db, *args):
        assert threading.get_ident() == db.owner
        entered.set(); assert release.wait(5)
        return 'editor'

    monkeypatch.setattr(dependencies, 'assert_member', member)
    monkeypatch.setattr(permissions, 'get_member_role', role)
    app = FastAPI()
    app.dependency_overrides[dependencies.get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: Owned
    dependency = dependencies.get_current_workspace if kind == 'context' else dependencies.require_workspace_role('editor')

    @app.get('/context')
    async def read(workspace=Depends(dependency)):
        assert workspace._sa_instance_state.session is None
        return {'id': workspace.id}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.get('/context', headers={'X-Workspace-Id': '2'}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).json() == {'id': 2}
    assert len(opened) == (1 if kind == 'context' else 2) and closed == opened
