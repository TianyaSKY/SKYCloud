"""HTTP authentication resolves SQL and cached profile on its owning worker."""
import asyncio
import threading
from unittest.mock import Mock
import jwt
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from app.api import authentication, dependencies
from app.features.auth import sessions
from app.mcp import token_service
from app.infra.extensions import SECRET_KEY


@pytest.mark.parametrize('kind',['web','mcp'])
async def test_authentication_worker_ownership_and_fresh_role(monkeypatch, kind):
    loop_thread=threading.get_ident()
    entered,release=threading.Event(),threading.Event()
    opened,closed=[],[]
    profile={'id':1,'role':'admin'}
    class OwnedSession:
        def __init__(self):
            self.owner=threading.get_ident()
            assert self.owner!=loop_thread
            opened.append(self)
        def __enter__(self):return self
        def __exit__(self,*args):
            assert threading.get_ident()==self.owner
            closed.append(self)
    def valid(db,*args):
        assert threading.get_ident()==db.owner
        entered.set();assert release.wait(5)
        return True
    def role(db,*args):
        assert threading.get_ident()==db.owner
        return 'common'
    def cached(db,*args):
        assert threading.get_ident()==db.owner
        return profile
    monkeypatch.setattr(sessions,'valid_session',valid)
    monkeypatch.setattr(token_service,'get_active_mcp_token',valid)
    monkeypatch.setattr(authentication.account_repository,'current_role',role)
    monkeypatch.setattr(authentication.user_service,'_get_user_data',cached)
    payload={'sub':'1','ver':0}
    if kind=='mcp':payload['type']='mcp'
    credential=HTTPAuthorizationCredentials(scheme='Bearer',credentials=jwt.encode(payload,SECRET_KEY,algorithm='HS256'))
    pending=asyncio.create_task(dependencies.get_current_user(credential,Mock(),session_factory=OwnedSession))
    try:
        assert await asyncio.to_thread(entered.wait,2)
        async def probe():
            assert not release.is_set()
            return True
        assert await asyncio.wait_for(probe(),1) and not pending.done()
    finally:
        release.set();user=await pending
    assert user.id==1 and user.role=='common'
    assert user._sa_instance_state.session is None
    assert profile['role']=='admin'
    assert len(opened)==1 and closed==opened
