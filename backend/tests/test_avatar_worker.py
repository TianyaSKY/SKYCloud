"""Avatar adaptation and fresh role checks stay on the owning SQL worker."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.features.file import avatar_upload, router


@pytest.mark.parametrize('encoded', [False, True])
async def test_avatar_adaptation_and_sql_share_owner_without_blocking_loop(monkeypatch, encoded):
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    sessions, closed = [], []
    class Session:
        def __init__(self):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            sessions.append(self)
        def __enter__(self): return self
        def __exit__(self,*args):
            assert threading.get_ident() == self.owner
            closed.append(self)
    def role(db, actor):
        assert threading.get_ident() == db.owner and actor == 1
        return 'common'
    adapter = object()
    def adapt(*args):
        assert threading.get_ident() == sessions[0].owner
        entered.set()
        assert release.wait(5)
        return adapter
    def upload(db, actor, role, target, value):
        assert threading.get_ident() == db.owner
        assert (actor,role,target,value) == (1,'common',1,adapter)
        return {'avatar_url':'/api/share/issued'}
    monkeypatch.setattr(avatar_upload.account_repository,'current_role',role)
    monkeypatch.setattr(avatar_upload,'Base64UploadAdapter',adapt)
    monkeypatch.setattr(avatar_upload,'FastAPIUploadAdapter',adapt)
    monkeypatch.setattr(avatar_upload.service,'upload_avatar_for_user',upload)
    pending = asyncio.create_task(router.upload_avatar(1,SimpleNamespace(headers={}),
        avatar=None if encoded else SimpleNamespace(filename='avatar.png'),
        avatar_base64='eA==' if encoded else None,current_user=SimpleNamespace(id=1,role='admin'),session_factory=Session))
    try:
        assert await asyncio.to_thread(entered.wait,2)
        async def probe():
            assert not release.is_set()
            return True
        assert await asyncio.wait_for(probe(),1) and not pending.done()
    finally:
        release.set()
        result = await pending
    assert result['data']['avatar'] == '/api/share/issued'
    assert len(sessions) == 1 and closed == sessions


def test_current_role_denial_precedes_adaptation(monkeypatch):
    from contextlib import nullcontext
    from app.exceptions import PermissionDeniedError
    monkeypatch.setattr(avatar_upload.account_repository,'current_role',lambda *args:'common')
    adapter = Mock(side_effect=AssertionError('Denied actor must not adapt upload'))
    monkeypatch.setattr(avatar_upload,'Base64UploadAdapter',adapter)
    with pytest.raises(PermissionDeniedError):
        avatar_upload.upload_avatar_owned(lambda:nullcontext(object()),1,2,encoded='eA==')
    adapter.assert_not_called()
