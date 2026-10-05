"""Real Redis atomic rotation plus PostgreSQL authoritative account state."""
import concurrent.futures
import os
import time

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from tests.integration.test_runtime_security import infrastructure
from tests.integration.test_migrations import postgres
from entry.migrate import upgrade
from app.features.auth import refresh_sessions as refresh
from app.features.auth.service import generate_token
from app.infra.extensions import SECRET_KEY
from app.models.user import User

pytestmark = pytest.mark.skipif(os.getenv('RUN_REFRESH_SESSION_TESTS') != '1', reason='Opt-in real refresh session integration')


@pytest.fixture
def live(infrastructure, postgres, monkeypatch):
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    monkeypatch.setattr(refresh, 'redis_client', infrastructure['redis'])
    with factory() as db:
        user = User(username='isolated-browser-user', role='common', password_hash='isolated-test-only')
        db.add(user); db.commit()
        claims = jwt.decode(generate_token(user.id, user.auth_version), SECRET_KEY, algorithms=['HS256'])
        raw, response = refresh.create(db, claims)
    return factory, raw, response


def test_parallel_reuse_has_one_rotation_winner_and_revokes_the_family(live):
    factory, raw, _ = live
    def invoke(_):
        with factory() as db:
            try:
                return refresh.refresh(db, raw)
            except HTTPException as exc:
                assert exc.status_code == 401
                return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(invoke, range(24)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    _, response, _ = winners[0]
    claims = jwt.decode(response['token'], SECRET_KEY, algorithms=['HS256'])
    assert not refresh.access_active(claims)  # A consumed refresh credential was replayed.


def test_rotations_keep_absolute_expiry_hash_only_storage_and_new_connection_revocation(live):
    factory, raw, response = live
    claims = jwt.decode(response['token'], SECRET_KEY, algorithms=['HS256'])
    original_expiry = refresh.load_family(claims['sid'])['expires_at']
    secrets = [raw]
    for _ in range(5):
        with factory() as db:
            raw, response, lifetime = refresh.refresh(db, raw)
            secrets.append(raw)
        assert lifetime <= refresh.REFRESH_TTL
        assert refresh.load_family(claims['sid'])['expires_at'] == original_expiry
    for key in refresh.redis_client.scan_iter('web-auth:*'):
        value = refresh.redis_client.get(key)
        assert all(secret not in key and secret not in value for secret in secrets)
        assert 0 < refresh.redis_client.ttl(key) <= refresh.REFRESH_TTL
    with factory() as changed:
        changed.execute(update(User).where(User.id == int(claims['sub'])).values(auth_version=1)); changed.commit()
    with factory() as fresh:
        with pytest.raises(HTTPException) as exc:
            refresh.refresh(fresh, raw)
    assert exc.value.status_code == 401
