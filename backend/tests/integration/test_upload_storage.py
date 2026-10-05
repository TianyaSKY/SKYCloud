"""Opt-in authenticated HTTP uploads with isolated PostgreSQL and real MinIO.

Redis caching and RabbitMQ notifications retain the parent unit fixtures; these
checks specifically cover upload bytes, SQL ownership/quotas and object storage.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path
import time
from urllib.request import urlopen
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_migrations import postgres
from tests.integration.test_runtime_security import docker
from entry.migrate import upgrade
from app.api.factory import create_fastapi_app
from app.features.auth.service import generate_token
from app.features.file import service, upload_quota
from app.infra.extensions import get_db
from app.infra.storage import MinIOStorageClient
from app.models.upload_reservation import UploadReservation

pytestmark = pytest.mark.skipif(os.getenv('RUN_UPLOAD_STORAGE_TESTS') != '1', reason='Opt-in isolated PostgreSQL/MinIO upload integration')


@pytest.fixture
def minio_storage():
    image = os.getenv('SKYCLOUD_TEST_MINIO_IMAGE')
    assert image and image.startswith('sha256:'), 'Build scripts/build-minio-image.py and set SKYCLOUD_TEST_MINIO_IMAGE to its immutable ID'
    name = 'skycloud-upload-test-' + uuid.uuid4().hex[:12]
    access, secret = uuid.uuid4().hex, uuid.uuid4().hex + uuid.uuid4().hex
    try:
        docker('run', '-d', '--pull', 'never', '--name', name, '-e', 'MINIO_ROOT_USER=' + access,
               '-e', 'MINIO_ROOT_PASSWORD=' + secret, '-p', '127.0.0.1::9000',
               image, 'server', '/data')
        port = docker('port', name, '9000/tcp').stdout.strip().rsplit(':', 1)[1]
        endpoint = '127.0.0.1:' + port
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urlopen('http://' + endpoint + '/minio/health/cluster', timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(.1)
        else:
            raise AssertionError('Isolated MinIO did not start')
        client = MinIOStorageClient(endpoint, access, secret, 'upload-test-' + uuid.uuid4().hex[:12])
        client.integration_endpoint = endpoint
        client.integration_auth = (access, secret)
        yield client
    finally:
        docker('rm', '-f', name, check=False)


@pytest.fixture
def upload_api(postgres, minio_storage, monkeypatch, tmp_path):
    from app.features.folder import change_log
    from app.infra import storage
    upgrade(postgres)
    with postgres.begin() as conn:
        conn.execute(text("INSERT INTO users (id,username,password_hash,role) VALUES (1,'owner','x','common'),(2,'editor','x','common')"))
        conn.execute(text("INSERT INTO workspaces (id,name,owner_id) VALUES (1,'upload-space',1)"))
        conn.execute(text("INSERT INTO workspace_members (workspace_id,user_id,role) VALUES (1,1,'admin'),(1,2,'editor')"))
        conn.execute(text("INSERT INTO folder (name,workspace_id,parent_id) VALUES ('/',1,NULL)"))
    factory = sessionmaker(bind=postgres)
    monkeypatch.setattr(change_log, 'SessionLocal', factory)
    monkeypatch.setattr(storage, '_storage_client', minio_storage)
    monkeypatch.setattr(service, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(service, 'MULTIPART_ROOT', str(tmp_path / 'parts'))
    app = create_fastapi_app()
    def database():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = database
    headers = {'authorization': 'Bearer ' + generate_token(1), 'X-Workspace-Id': '1'}
    # Startup gating has separate real-PG tests; no unrelated global deployment
    # connection is used by this HTTP fixture.
    return TestClient(app), headers, tmp_path


def test_authenticated_upload_download_multipart_owner_and_limits(upload_api, postgres, minio_storage, monkeypatch):
    client, headers, root = upload_api
    response = client.post('/api/files', headers=headers, files={'file': ('small.txt', b'hello', 'text/plain')})
    assert response.status_code == 201, response.text
    record = response.json()
    assert record['file_size'] == 5
    assert client.get(f"/api/files/{record['id']}/download",
                      params={'token': generate_token(1)},
                      headers={'X-Workspace-Id': '1'}).status_code == 401
    response = client.get(f"/api/files/{record['id']}/download", headers=headers)
    assert response.status_code == 200 and response.content == b'hello'
    monkeypatch.setattr(service, 'MAX_UPLOAD_SIZE', 5)
    response = client.post('/api/files', headers=headers, files={'file': ('large.txt', b'too large', 'text/plain')})
    assert response.status_code == 413
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 1
    monkeypatch.setattr(service, 'MAX_UPLOAD_SIZE', 1024)
    body = b'12345678'
    response = client.post('/api/files/multipart/init', headers=headers, json={
        'filename': 'multipart.txt', 'total_size': len(body), 'chunk_size': 4,
        'content_hash': hashlib.sha256(body).hexdigest()})
    assert response.status_code == 200, response.text
    rid = response.json()['upload_id']
    other = {**headers, 'authorization': 'Bearer ' + generate_token(2)}
    assert client.get('/api/files/multipart/' + rid, headers=other).status_code == 403
    response = client.post('/api/files/multipart/chunk', headers=other,
        data={'upload_id': rid, 'chunk_index': 0}, files={'chunk': ('part', body[:4])})
    assert response.status_code == 403
    for index in range(2):
        response = client.post('/api/files/multipart/chunk', headers=headers,
            data={'upload_id': rid, 'chunk_index': index}, files={'chunk': ('part', body[index*4:(index+1)*4])})
        assert response.status_code == 200, response.text
    response = client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': rid})
    assert response.status_code == 201, response.text
    assert client.get(f"/api/files/{response.json()['id']}/download", headers=headers).content == body
    assert client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': rid}).status_code == 409
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 2
        assert conn.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == 2
        assert conn.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 2
    assert not (root / 'uploads' / '.staging').exists()


def test_worker_reconciles_real_orphan_object_and_retains_committed_object(upload_api, postgres, minio_storage):
    client, headers, root = upload_api
    response = client.post('/api/files', headers=headers, files={'file': ('keep.txt', b'keep')})
    assert response.status_code == 201, response.text
    minio_storage.upload_data(b'orphan', 'crash-orphan')
    with Session(postgres) as session:
        row = upload_quota.reserve(session, 1, 1, 6, details={'object_name': 'crash-orphan'})
        rid = row.id
        upload_quota.cancel(session, 1, 1, rid)
        service.cleanup_expired_uploads(session=session)
        assert session.get(UploadReservation, rid).cleaned_at is not None
    assert not minio_storage.file_exists('crash-orphan')
    assert client.get(f"/api/files/{response.json()['id']}/download", headers=headers).content == b'keep'
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 1


def test_real_database_commit_failure_removes_uploaded_object(upload_api, postgres, minio_storage):
    client, headers, root = upload_api
    with postgres.begin() as conn:
        conn.execute(text("ALTER TABLE files ADD CONSTRAINT reject_test_file CHECK (name <> 'reject.txt')"))
    response = client.post('/api/files', headers=headers, files={'file': ('reject.txt', b'12345')})
    assert response.status_code == 500
    assert response.json()['message'] == 'Internal server error'
    assert list(minio_storage._client.list_objects(minio_storage._bucket)) == []
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 0
        assert conn.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
        assert conn.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == 1
    assert not (root / 'uploads' / '.staging').exists()


def test_real_storage_rejection_holds_capacity_until_reconciliation(upload_api, postgres, minio_storage, monkeypatch):
    from minio import Minio
    from app.exceptions import BusinessRuleError
    client, headers, root = upload_api
    original_client = minio_storage._client
    # Exercise a real S3 authentication failure, including failed cleanup, without
    # replacing any storage operation with a mock.
    minio_storage._client = Minio(minio_storage.integration_endpoint,
        access_key='invalid-test-user', secret_key='invalid-test-password', secure=False)
    monkeypatch.setattr(upload_quota, 'USER_CAPACITY', 5)
    response = client.post('/api/files', headers=headers, files={'file': ('failure.txt', b'12345')})
    assert response.status_code == 500
    assert response.json()['message'] == 'Internal server error'
    with Session(postgres) as session:
        row = session.query(UploadReservation).one()
        assert row.status == 'cancelled' and row.cleaned_at is None
        with pytest.raises(BusinessRuleError, match='capacity'):
            upload_quota.reserve(session, 1, 1, 1)
        session.rollback()
    minio_storage._client = original_client
    with Session(postgres) as session:
        service.cleanup_expired_uploads(session=session)
        assert session.query(UploadReservation).one().cleaned_at is not None
        assert upload_quota.reserve(session, 1, 1, 5).size == 5
    assert list(original_client.list_objects(minio_storage._bucket)) == []
    assert not (root / 'uploads' / '.staging').exists()


def test_process_crash_after_real_put_is_reconciled(upload_api, postgres, minio_storage):
    from datetime import timedelta
    client, headers, root = upload_api
    script = """
import os
from io import BytesIO
saved_environment = dict(os.environ)
import tests.conftest  # Only optional AI/Docker libraries are stubbed.
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from werkzeug.datastructures import FileStorage
from app.features.file import service
from app.infra.storage import MinIOStorageClient
engine = create_engine(saved_environment['DATABASE_URL'])
service.UPLOAD_FOLDER = saved_environment['UPLOAD_FOLDER']
storage = MinIOStorageClient(saved_environment['MINIO_ENDPOINT'], saved_environment['MINIO_ACCESS_KEY'],
    saved_environment['MINIO_SECRET_KEY'], saved_environment['MINIO_BUCKET'])
service.get_storage_client = lambda: storage
service._persist_file_record = lambda *args, **kwargs: os._exit(31)
with Session(engine) as session:
    service.create_file(session, FileStorage(stream=BytesIO(b'crashed upload'), filename='crash.txt'),
                        {'workspace_id': 1, 'uploader_id': 1})
"""
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, 'DATABASE_URL': postgres.url.render_as_string(hide_password=False),
             'UPLOAD_FOLDER': str(root / 'uploads'), 'MINIO_ENDPOINT': minio_storage.integration_endpoint,
             'MINIO_ACCESS_KEY': minio_storage.integration_auth[0], 'MINIO_SECRET_KEY': minio_storage.integration_auth[1],
             'MINIO_BUCKET': minio_storage._bucket}, capture_output=True, timeout=30)
    assert result.returncode == 31, result.stderr.decode()
    with Session(postgres) as session:
        row = session.query(UploadReservation).one()
        rid, object_name, stage_id = row.id, row.details['object_name'], row.details['stage_id']
        assert row.status == 'active'
        assert minio_storage.file_exists(object_name)
        stage = root / 'uploads' / '.staging' / stage_id
        assert stage.exists()
        row.expires_at = upload_quota.utc_now() - timedelta(seconds=1)
        session.commit()
        old = time.time() - upload_quota.TTL_SECONDS - 1
        os.utime(stage, (old, old))
        service.cleanup_expired_uploads(session=session)
        assert session.get(UploadReservation, rid).status == 'expired'
        assert session.get(UploadReservation, rid).cleaned_at is not None
        assert not stage.exists()
        assert not minio_storage.file_exists(object_name)
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 0
