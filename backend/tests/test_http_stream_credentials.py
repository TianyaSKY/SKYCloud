"""Continued HTTP stream authorization checks signed scope and live state."""
import asyncio
import time
import jwt
import pytest
from app.api.stream_credentials import StreamCredential
from app.exceptions import AuthenticationError
from app.features.chat import repository, stream_authorization as guard
from app.infra.extensions import SECRET_KEY
from app.models.mcp_token import McpToken
from app.infra.datetime_utils import utc_now
from datetime import timedelta


@pytest.mark.parametrize('fault', ['expired','signature','subject','scope','version'])
def test_invalid_or_revoked_web_stream(session, test_user, fault):
    claims = {'sub': str(test_user.id), 'ver': 0, 'exp': int(time.time()) + 60}
    if fault == 'expired': claims['exp'] = int(time.time()) - 1
    if fault == 'subject': claims['sub'] = str(test_user.id + 1)
    if fault == 'scope': claims['type'] = 'runtime_llm'
    if fault == 'version': claims['ver'] = 99
    token = jwt.encode(claims, 'wrong-key' * 8 if fault == 'signature' else SECRET_KEY, algorithm='HS256')
    with pytest.raises(AuthenticationError): StreamCredential(token, test_user.id).validate(session, test_user.id)


def test_web_stream_observes_auth_version_change(session, test_user):
    token = jwt.encode({'sub':str(test_user.id),'ver':0,'exp':int(time.time())+60}, SECRET_KEY, algorithm='HS256')
    credential = StreamCredential(token, test_user.id)
    credential.validate(session, test_user.id)
    test_user.auth_version += 1
    session.commit()
    with pytest.raises(AuthenticationError): credential.validate(session, test_user.id)
    assert token not in repr(credential)


@pytest.mark.parametrize('fault', ['revoked','expired','foreign-owner'])
def test_mcp_stream_reads_token_state_without_commit(session, test_user, admin_user, monkeypatch, fault):
    token = jwt.encode({'sub':str(test_user.id),'type':'mcp','exp':int(time.time())+60}, SECRET_KEY, algorithm='HS256')
    row = McpToken(user_id=test_user.id, token_hash=McpToken.hash_token(token), token_preview='test',
                   expires_at=utc_now()+timedelta(minutes=1))
    session.add(row); session.commit()
    credential = StreamCredential(token, test_user.id)
    credential.validate(session, test_user.id)
    if fault == 'revoked': row.revoked_at = utc_now()
    elif fault == 'expired': row.expires_at = utc_now()-timedelta(seconds=1)
    else: row.user_id = admin_user.id
    session.flush()
    monkeypatch.setattr(session, 'commit', lambda: pytest.fail('poll must not commit'))
    with pytest.raises(AuthenticationError): credential.validate(session, test_user.id)
    assert row.last_used_at is None


@pytest.mark.parametrize('quiet', [False, True])
async def test_guard_closes_model_on_live_credential_revocation(monkeypatch, quiet):
    revoked, closed = [False], []
    waiting = asyncio.Event()
    class Credential:
        def validate(self, session, user_id):
            if revoked[0]: raise AuthenticationError('revoked')
    class Owned:
        def close(self): pass
    monkeypatch.setattr(repository,'assert_member',lambda *args:None)
    monkeypatch.setattr(guard,'POLL_SECONDS',0.01)
    async def source():
        try:
            yield 'allowed'
            waiting.set()
            if quiet: await asyncio.Event().wait()
            revoked[0] = True
            yield 'private-late'
        finally: closed.append(True)
    received=[]
    async def collect():
        async for event in guard.authorized_events(source(), Owned, 1, 1, Credential()): received.append(event)
    pending=asyncio.create_task(collect())
    if quiet:
        await asyncio.wait_for(waiting.wait(),1)
        revoked[0]=True
    with pytest.raises(AuthenticationError): await asyncio.wait_for(pending,2)
    assert received == ['allowed'] and closed == [True]
