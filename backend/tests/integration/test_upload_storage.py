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
from app.infra.extensions import get_db, get_session_factory
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
    app.dependency_overrides[get_session_factory] = lambda: factory
    headers = {'authorization': 'Bearer ' + generate_token(1), 'X-Workspace-Id': '1'}
    # Startup gating has separate real-PG tests; no unrelated global deployment
    # connection is used by this HTTP fixture.
    return TestClient(app), headers, tmp_path


def test_authenticated_upload_download_multipart_owner_and_limits(upload_api, postgres, minio_storage, monkeypatch):
    client, headers, root = upload_api
    response = client.post('/api/files', headers=headers, files={'file': ('small.txt', b'hello', 'text/plain')})
    assert response.status_code == 201, response.text
    record = response.json()["data"]
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
    rid = response.json()['data']['upload_id']
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
    assert client.get(f"/api/files/{response.json()['data']['id']}/download", headers=headers).content == body
    assert client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': rid}).status_code == 409
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 2
        assert conn.scalar(text('SELECT count(*) FROM file_task_outbox WHERE delivered_at IS NULL')) == 2
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
    assert client.get(f"/api/files/{response.json()['data']['id']}/download", headers=headers).content == b'keep'
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 1


def test_real_database_commit_failure_removes_uploaded_object(upload_api, postgres, minio_storage):
    client, headers, root = upload_api
    with postgres.begin() as conn:
        conn.execute(text("ALTER TABLE files ADD CONSTRAINT reject_test_file CHECK (name <> 'reject.txt')"))
    response = client.post('/api/files', headers=headers, files={'file': ('reject.txt', b'12345')})
    assert response.status_code == 500
    assert response.json()['error']['message'] == 'Internal server error'
    assert list(minio_storage._client.list_objects(minio_storage._bucket)) == []
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 0
        assert conn.scalar(text('SELECT count(*) FROM file_task_outbox')) == 0
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
    assert response.json()['error']['message'] == 'Internal server error'
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


@pytest.mark.parametrize('delete_failure', [False, True])
def test_real_batch_staging_failure_compensation_and_worker_recovery(upload_api, postgres, minio_storage, monkeypatch, delete_failure):
    """Explicit second-stage allocation fault; actual PG/MinIO compensation."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.exceptions import BusinessRuleError, ServiceOperationError
    from app.infra.upload_adapter import FastAPIUploadAdapter
    from minio import Minio
    client, headers, root = upload_api
    monkeypatch.setattr(upload_quota, 'USER_CAPACITY', 10)
    original_stage = service.StagedUpload
    healthy_client = minio_storage._client
    calls = 0
    def stage(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if delete_failure:
                minio_storage._client = Minio(minio_storage.integration_endpoint, access_key='invalid', secret_key='invalid-secret', secure=False)
            raise OSError('private-staging-allocation-fault')
        return original_stage(*args, **kwargs)
    monkeypatch.setattr(service, 'StagedUpload', stage)
    uploads = [FastAPIUploadAdapter(UploadFile(filename=name, file=BytesIO(b'12345'))) for name in ['one.txt','two.txt']]
    try:
        with Session(postgres) as db:
            with pytest.raises(ServiceOperationError, match='Failed to upload file'):
                service.batch_create_files(db, uploads, {'workspace_id':1,'uploader_id':1})
            assert db.scalar(text('SELECT count(*) FROM files')) == 0
            assert db.scalar(text('SELECT count(*) FROM file_task_outbox')) == 0
            assert db.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
            assert db.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == 1
            if delete_failure:
                with pytest.raises(BusinessRuleError, match='capacity'):
                    upload_quota.reserve(db, 1, 1, 10)
                db.rollback()
            minio_storage._client = healthy_client
            assert len(list(healthy_client.list_objects(minio_storage._bucket))) == int(delete_failure)
            service.cleanup_expired_uploads(session=db)
            assert db.scalar(text('SELECT count(*) FROM upload_reservations WHERE cleaned_at IS NULL')) == 0
            assert upload_quota.reserve(db, 1, 1, 10).size == 10
        assert list(healthy_client.list_objects(minio_storage._bucket)) == []
        assert not (root / 'uploads' / '.staging').exists()
    finally:
        minio_storage._client = healthy_client


@pytest.mark.parametrize('kind', ['single', 'batch', 'multipart'])
def test_real_upload_committed_before_ack_failure_keeps_downloadable_objects(upload_api, postgres, minio_storage, monkeypatch, kind):
    """Explicit post-COMMIT acknowledgement fault seam; actual SQL and objects."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.exceptions import ServiceOperationError
    from app.infra.upload_adapter import FastAPIUploadAdapter
    client, headers, root = upload_api
    attribute = 'insert_files' if kind == 'batch' else 'insert_file'
    insert = getattr(service.repository, attribute)
    fail = False
    def persist(*args, **kwargs):
        nonlocal fail
        result = insert(*args, **kwargs)
        fail = True
        return result
    monkeypatch.setattr(service.repository, attribute, persist)
    def upload(body):
        return FastAPIUploadAdapter(UploadFile(filename='x.txt', file=BytesIO(body)))
    with Session(postgres) as db:
        commit = db.commit
        def acknowledged_commit():
            nonlocal fail
            committed_file = fail
            fail = False
            commit()
            if committed_file:
                raise OSError('private-commit-acknowledgement-fault')
        monkeypatch.setattr(db, 'commit', acknowledged_commit)
        with pytest.raises(ServiceOperationError):
            if kind == 'single':
                service.create_file(db, upload(b'12345'), {'workspace_id':1,'uploader_id':1})
            elif kind == 'batch':
                service.batch_create_files(db, [upload(b'12345'),upload(b'67890')], {'workspace_id':1,'uploader_id':1})
            else:
                rid = service.init_multipart_upload(db, 1, 1, {'filename':'x.txt','total_size':5})['upload_id']
                service.save_multipart_chunk(db, 1, 1, rid, 0, upload(b'12345'))
                service.complete_multipart_upload(db, 1, 1, rid)
    expected = 2 if kind == 'batch' else 1
    with Session(postgres) as observer:
        files = observer.execute(text('SELECT id,file_path FROM files ORDER BY id')).all()
        assert len(files) == expected
        assert observer.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='completed'")) == expected
        assert observer.scalar(text('SELECT count(*) FROM file_task_outbox')) == expected
        assert observer.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == expected
        service.cleanup_expired_uploads(session=observer)
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == expected
    for file in files:
        response = client.get(f'/api/files/{file.id}/download', headers=headers)
        assert response.status_code == 200
        assert response.content in [b'12345',b'67890']
    assert not (root / 'uploads' / '.staging').exists()


@pytest.mark.parametrize('rollback_fault', [False, True])
def test_real_batch_cancel_sql_rejection_preserves_error_and_recovers_remaining_entries(upload_api, postgres, minio_storage, monkeypatch, rollback_fault):
    """Actual PG cancellation constraint rejection; optional rollback fault seam."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.exceptions import BusinessRuleError
    from app.infra.upload_adapter import FastAPIUploadAdapter
    client, headers, root = upload_api
    monkeypatch.setattr(upload_quota, 'USER_CAPACITY', 10)
    save = service.save_bounded
    original = BusinessRuleError('original upload failure')
    calls = 0
    first_id = 'a' * 32
    with postgres.begin() as sql:
        sql.execute(text("ALTER TABLE upload_reservations ADD CONSTRAINT reject_first_cancel CHECK (id <> 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' OR status <> 'cancelled')"))
    stage = service.StagedUpload
    stages = 0
    def fixed_stage(*args, **kwargs):
        nonlocal stages
        stages += 1
        return stage(*args, stage_id=chr(96 + stages) * 32, **kwargs)
    monkeypatch.setattr(service, 'StagedUpload', fixed_stage)
    def save_then_fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise original
        return save(*args, **kwargs)
    monkeypatch.setattr(service, 'save_bounded', save_then_fail)
    with Session(postgres) as db:
        rollback = db.rollback
        failed = False
        def reset():
            nonlocal failed
            if rollback_fault and not failed:
                failed = True
                raise OSError('private-rollback-fault')
            return rollback()
        monkeypatch.setattr(db, 'rollback', reset)
        uploads = [FastAPIUploadAdapter(UploadFile(filename='x.txt', file=BytesIO(b'12345'))) for _ in range(3)]
        with pytest.raises(BusinessRuleError) as caught:
            service.batch_create_files(db, uploads, {'workspace_id':1,'uploader_id':1})
        assert caught.value is original
    with Session(postgres) as observer:
        rows = observer.execute(text('SELECT id,status FROM upload_reservations')).all()
        assert len(rows) == 2
        assert {r.status for r in rows} == {'active','cancelled'}
        assert next(r for r in rows if r.id == first_id).status == 'active'
        assert observer.scalar(text('SELECT count(*) FROM files')) == 0
        assert observer.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == 2
    assert list(minio_storage._client.list_objects(minio_storage._bucket)) == []
    with postgres.begin() as sql:
        sql.execute(text('ALTER TABLE upload_reservations DROP CONSTRAINT reject_first_cancel'))
        sql.execute(text("UPDATE upload_reservations SET expires_at=NOW()-INTERVAL '1 second' WHERE id=:id"),{'id':first_id})
    with Session(postgres) as observer:
        service.cleanup_expired_uploads(session=observer)
        assert observer.scalar(text('SELECT count(*) FROM upload_reservations WHERE cleaned_at IS NULL')) == 0
        assert upload_quota.reserve(observer, 1, 1, 10).size == 10
    assert not (root / 'uploads' / '.staging').exists()


@pytest.mark.parametrize('format', ['multipart','form','json'])
def test_real_avatar_upload_formats_update_account_and_keep_share_bytes(upload_api, postgres, minio_storage, format):
    client, headers, root = upload_api
    import base64
    body = b'avatar-bytes'
    value = 'data:image/png;base64,' + base64.b64encode(body).decode()
    kwargs = {'files':{'avatar':('avatar.png',body,'image/png')}} if format == 'multipart' else ({'data':{'avatar_base64':value}} if format == 'form' else {'json':{'avatar':value}})
    response = client.post('/api/files/upload/avatar/1',headers=headers,**kwargs)
    assert response.status_code == 200,response.text
    data = response.json()['data']
    assert data['avatar'] == data['avatar_url']
    with postgres.connect() as sql:
        assert sql.scalar(text('SELECT avatar FROM users WHERE id=1')) == data['avatar_url']
        assert sql.scalar(text('SELECT count(*) FROM files')) == 1
    shared = client.get(data['avatar_url'])
    assert shared.status_code == 200 and shared.content == body
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 1


def test_real_avatar_worker_rejects_stale_admin_role_before_object_put(upload_api, postgres, minio_storage, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.dependencies import get_current_user
    from app.exceptions import register_exception_handlers
    from app.features.file import router
    client, headers, root = upload_api
    with postgres.begin() as sql:
        sql.execute(text("UPDATE users SET role='admin' WHERE id=1"))
    app=FastAPI();register_exception_handlers(app);app.include_router(router.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=1,role='admin')
    app.dependency_overrides[get_session_factory]=lambda:sessionmaker(bind=postgres)
    with postgres.begin() as sql:
        sql.execute(text("UPDATE users SET role='common' WHERE id=1"))
    response=TestClient(app).post('/files/upload/avatar/2',json={'avatar':'data:image/png;base64,eA=='})
    assert response.status_code == 403,response.text
    assert list(minio_storage._client.list_objects(minio_storage._bucket)) == []
    with postgres.connect() as sql:
        assert sql.scalar(text('SELECT count(*) FROM files')) == 0
        assert sql.scalar(text('SELECT count(*) FROM upload_reservations')) == 0


@pytest.mark.parametrize('failure',['share','profile','ack'])
def test_real_avatar_publication_sql_failure_and_commit_ack_outcome(upload_api,postgres,minio_storage,monkeypatch,failure):
    client,headers,root=upload_api
    with postgres.begin() as sql:
        sql.execute(text("UPDATE users SET avatar='/existing-avatar' WHERE id=1"))
        if failure=='share':
            sql.execute(text('ALTER TABLE shares ADD CONSTRAINT reject_avatar_share CHECK (file_id < 0)'))
        elif failure=='profile':
            sql.execute(text("ALTER TABLE users ADD CONSTRAINT reject_avatar_url CHECK (avatar NOT LIKE '/api/share/%')"))
    if failure=='ack':
        insert=service.repository.insert_file
        def persist(db,*args,**kwargs):
            result=insert(db,*args,**kwargs);db.info['fail_avatar_ack']=True
            return result
        class AckSession(Session):
            def commit(self):
                fail=self.info.pop('fail_avatar_ack',False)
                super().commit()
                if fail:raise OSError('private-avatar-commit-ack-fault')
        monkeypatch.setattr(service.repository,'insert_file',persist)
        client.app.dependency_overrides[get_session_factory]=lambda:sessionmaker(bind=postgres,class_=AckSession)
    response=client.post('/api/files/upload/avatar/1',headers=headers,files={'avatar':('avatar.png',b'avatar-content','image/png')})
    assert response.status_code==500,response.text
    expected=int(failure=='ack')
    with postgres.connect() as sql:
        assert sql.scalar(text('SELECT count(*) FROM files'))==expected
        assert sql.scalar(text('SELECT count(*) FROM shares'))==expected
        assert sql.scalar(text('SELECT count(*) FROM file_task_outbox'))==expected
        assert sql.scalar(text('SELECT sum(attempts) FROM upload_daily_usage'))==1
        assert sql.scalar(text("SELECT status FROM upload_reservations"))==('completed' if expected else 'cancelled')
        url=sql.scalar(text('SELECT avatar FROM users WHERE id=1'))
        assert (url.startswith('/api/share/'))==bool(expected)
        if not expected:assert url=='/existing-avatar'
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket)))==expected
    if expected:
        shared=client.get(url)
        assert shared.status_code==200 and shared.content==b'avatar-content'
    assert not (root/'uploads'/'.staging').exists()


@pytest.mark.parametrize('metadata_rejection',[False,True])
def test_real_upload_cleanup_close_fault_does_not_replace_sql_outcome(upload_api,postgres,minio_storage,monkeypatch,metadata_rejection):
    client,headers,root=upload_api
    if metadata_rejection:
        with postgres.begin() as sql:sql.execute(text("ALTER TABLE files ADD CONSTRAINT reject_close_test CHECK (name <> 'close.txt')"))
    close=service.StagedUpload.close
    closed=[]
    def fault(stage):
        close(stage);closed.append(stage.handle.closed)
        raise OSError('private-close-fault')
    monkeypatch.setattr(service.StagedUpload,'close',fault)
    response=client.post('/api/files',headers=headers,files={'file':('close.txt',b'body')})
    assert response.status_code==(500 if metadata_rejection else 201),response.text
    expected=int(not metadata_rejection)
    assert closed==[True]
    with postgres.connect() as sql:
        assert sql.scalar(text('SELECT count(*) FROM files'))==expected
        assert sql.scalar(text('SELECT count(*) FROM file_task_outbox'))==expected
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket)))==expected
    if expected:assert client.get(f"/api/files/{response.json()['data']['id']}/download",headers=headers).content==b'body'
    assert not (root/'uploads'/'.staging').exists()


@pytest.mark.parametrize('kind', ['single', 'batch', 'multipart', 'instant'])
def test_upload_publication_rejects_committed_role_loss(upload_api, postgres, minio_storage, monkeypatch, kind):
    """Actual SQL/object bytes; explicit boundary hook commits role loss."""
    client, headers, root = upload_api
    baseline_files = baseline_tasks = baseline_objects = 0
    if kind == 'instant':
        source = client.post('/api/files', headers=headers, files={'file': ('source.txt', b'abc')})
        assert source.status_code == 201, source.text
        with postgres.connect() as db:
            baseline_files = db.scalar(text('SELECT count(*) FROM files'))
            baseline_tasks = db.scalar(text('SELECT count(*) FROM file_task_outbox'))
        baseline_objects = len(list(minio_storage._client.list_objects(minio_storage._bucket)))
        reserve = service.upload_quota.reserve
        def reserve_then_demote(*args, **kwargs):
            row = reserve(*args, **kwargs)
            with postgres.begin() as independent:
                independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
            return row
        monkeypatch.setattr(service.upload_quota, 'reserve', reserve_then_demote)
    else:
        put = minio_storage.upload_file
        puts = 0
        def put_then_demote(*args, **kwargs):
            nonlocal puts
            result = put(*args, **kwargs)
            puts += 1
            if puts == (2 if kind == 'batch' else 1):
                with postgres.begin() as independent:
                    independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
            return result
        monkeypatch.setattr(minio_storage, 'upload_file', put_then_demote)
    if kind == 'single':
        response = client.post('/api/files', headers=headers, files={'file': ('single.txt', b'abc')})
    elif kind == 'batch':
        response = client.post('/api/files/batch', headers=headers,
                               files=[('files', ('first.txt', b'abc')), ('files', ('second.txt', b'def'))])
    elif kind == 'multipart':
        admitted = client.post('/api/files/multipart/init', headers=headers, json={'filename': 'parts.txt', 'total_size': 3})
        assert admitted.status_code == 200, admitted.text
        upload_id = admitted.json()['data']['upload_id']
        chunk = client.post('/api/files/multipart/chunk', headers=headers, data={'upload_id': upload_id, 'chunk_index': '0'},
                            files={'chunk': ('part', b'abc')})
        assert chunk.status_code == 200, chunk.text
        # HTTP entry already holds membership through completion. Exercise the
        # shared service boundary directly without that extra request lock.
        from app.exceptions import PermissionDeniedError
        with Session(postgres) as db:
            with pytest.raises(PermissionDeniedError):
                service.complete_multipart_upload(db, 1, 1, upload_id)
        response = None
    else:
        response = client.post('/api/files/preflight', headers=headers,
                               json={'filename': 'clone.txt', 'total_size': 3, 'content_hash': hashlib.sha256(b'abc').hexdigest()})
    if response is not None:
        assert response.status_code == 403, response.text
    with postgres.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM files')) == baseline_files
        assert db.scalar(text('SELECT count(*) FROM file_task_outbox')) == baseline_tasks
        assert db.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
        assert db.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='cancelled'")) == (2 if kind == 'batch' else 1)
        assert db.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == (2 if kind in {'batch', 'instant'} else 1)
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == baseline_objects
    if kind == 'instant':
        assert client.get('/api/files/' + str(source.json()['data']['id']) + '/download', headers=headers).content == b'abc'
