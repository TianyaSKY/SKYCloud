"""HTTP profile SQL, fresh authorization and cached data stay on one worker."""
import asyncio
import threading
from unittest.mock import Mock
import pytest
from app.features.auth import user_service
from app.exceptions import PermissionDeniedError


@pytest.mark.parametrize('actor_role',['self','admin','common'])
async def test_profile_worker_ownership_and_current_authorization(monkeypatch,actor_role):
    loop=threading.get_ident()
    opened,closed=[],[]
    entered,release=threading.Event(),threading.Event()
    target=1 if actor_role=='self' else 2
    cached={'id':target,'role':'admin'}
    class Owned:
        def __init__(self):
            self.thread=threading.get_ident();assert self.thread!=loop
            opened.append(self)
        def __enter__(self):return self
        def __exit__(self,*args):
            assert threading.get_ident()==self.thread
            closed.append(self)
    def role(db,id):
        assert threading.get_ident()==db.thread
        if id==1:
            entered.set();assert release.wait(5)
            return 'admin' if actor_role=='admin' else 'common'
        return 'common'
    def profile(db,id):
        assert threading.get_ident()==db.thread
        return cached
    reader=Mock(side_effect=profile)
    monkeypatch.setattr(user_service.account_repository,'current_role',role)
    monkeypatch.setattr(user_service,'_get_user_data',reader)
    pending=asyncio.create_task(user_service.get_user(None,target,session_factory=Owned,actor_id=1))
    try:
        assert await asyncio.to_thread(entered.wait,2)
        async def probe():
            assert not release.is_set()
            return True
        assert await asyncio.wait_for(probe(),1) and not pending.done()
    finally:release.set()
    if actor_role=='common':
        with pytest.raises(PermissionDeniedError):await pending
        reader.assert_not_called()
    else:
        user=await pending
        assert user.id==target and user.role=='common' and user._sa_instance_state.session is None
        assert cached['role']=='admin'
    assert len(opened)==1 and closed==opened
