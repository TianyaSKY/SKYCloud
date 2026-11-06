from typing import Any, cast
# pyright: reportPrivateUsage=false
"""隔离 PostgreSQL 与真实 MinIO 的自选认证 HTTP 上传测试."""
import hashlib
import json
import asyncio
import threading
import os
import subprocess
import sys
from pathlib import Path
import time
from urllib.request import urlopen
import uuid

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_migrations import postgres
from tests.integration.test_runtime_security import docker
from entry.migrate import upgrade
from app.api.factory import create_fastapi_app
from app.features.auth.service import generate_token
from app.features.file import service, upload_config, upload_quota
from app.infra.extensions import get_db, get_session_factory
from app.infra.storage import MinIOStorageClient
from app.infra.upload_adapter import FastAPIUploadAdapter
from app.models.upload_reservation import UploadReservation

__all__ = ["postgres", "minio_storage"]

pytestmark = pytest.mark.skipif(os.getenv('RUN_UPLOAD_STORAGE_TESTS') != '1', reason='Opt-in isolated PostgreSQL/MinIO upload integration')


@pytest.mark.parametrize('body', [b'a' * (512 * 1024 + 100), ('中' * 200000).encode(), b''],
                         ids=['ascii', 'utf8', 'empty'])
async def test_mcp_text_read_uses_bounded_minio_range(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any, body: Any) -> None:
    """真实 SQL/MinIO 工具 worker; 显式上下文, 无 SDK 传输断言."""
    from app.mcp import server
    from app.mcp.context import McpRequestContext, set_mcp_context, reset_mcp_context
    minio_storage.upload_data(body, 'prefix-object', 'text/plain')
    with postgres.begin() as conn:
        file_id = conn.scalar(text("""INSERT INTO files
            (name,file_path,file_size,mime_type,status,workspace_id,uploader_id,parent_id)
            VALUES ('prefix.txt','prefix-object',:size,'text/plain','success',1,1,1) RETURNING id"""),
                              {'size': len(body)})
    monkeypatch.setattr(server, 'SessionLocal', sessionmaker(bind=postgres))
    requests: list[Any] = []
    reads: list[tuple[Any, int]] = []
    released: list[str] = []
    get_object = minio_storage._client.get_object
    def ranged_object(*args: Any, **kwargs: Any):
        response = get_object(*args, **kwargs)
        requests.append((kwargs.get('length'), response.status, response.headers.get('Content-Range')))
        read, close, release = response.read, response.close, response.release_conn
        def bounded_read(amount: Any):
            result = read(amount)
            reads.append((amount, len(result)))
            return result
        def close_body():
            close()
            released.append('close')
        def release_body():
            release()
            released.append('release')
        response.read, response.close, response.release_conn = bounded_read, close_body, release_body
        return response
    monkeypatch.setattr(minio_storage._client, 'get_object', ranged_object)
    def full_download(*args: Any):
        pytest.fail('MCP text read must not download the complete object')
    monkeypatch.setattr(minio_storage, 'download_file', full_download)
    token = set_mcp_context(McpRequestContext(user_id=1, workspace_id=1, role='admin'))
    try:
        result = json.loads(await server.read_file_content(file_id))
    finally:
        reset_mcp_context(token)
    assert result['file_size'] == len(body)
    assert result['truncated'] == (len(body) > server._MAX_READ_BYTES)
    assert result['content'] == body[:server._MAX_READ_BYTES].decode('utf-8', errors='ignore')
    if body:
        assert requests == [(server._MAX_READ_BYTES, 206,
                             f'bytes 0-{server._MAX_READ_BYTES - 1}/{len(body)}')]
        assert reads == [(server._MAX_READ_BYTES, server._MAX_READ_BYTES)]
        assert released[-2:] == ['close', 'release']
    else:
        assert requests == reads == released == []






@pytest.mark.parametrize('alias', ['root', 'workspace', 'upload', 'chunks'])
@pytest.mark.parametrize('phase', ['init', 'chunk'])
def test_multipart_writes_do_not_follow_parent_aliases(upload_api: Any, postgres: Any, minio_storage: Any, alias: Any, phase: Any) -> None:
    client, headers, root = upload_api
    rid = uuid.uuid4().hex
    if phase == 'chunk':
        admitted = client.post('/api/files/multipart/init', headers=headers,
                               json={'filename': 'safe.txt', 'total_size': 3, 'upload_id': rid})
        assert admitted.status_code == 200
    parts = root / 'uploads' / '.multipart'
    relative = ['1', rid, 'chunks']
    level = ['root', 'workspace', 'upload', 'chunks'].index(alias)
    original = parts.joinpath(*relative[:level])
    original.mkdir(parents=True, exist_ok=True)
    foreign = root / 'foreign'
    foreign_chunks = foreign.joinpath(*relative[level:])
    foreign_chunks.mkdir(parents=True)
    private = foreign_chunks / '0.part'
    private.write_bytes(b'previous')
    before = sorted(str(path.relative_to(foreign)) for path in foreign.rglob('*'))
    retired = root / 'retired'
    original.rename(retired)
    original.symlink_to(foreign, target_is_directory=True)
    if phase == 'init':
        rejected = client.post('/api/files/multipart/init', headers=headers,
                               json={'filename': 'safe.txt', 'total_size': 3, 'upload_id': rid})
    else:
        rejected = client.post('/api/files/multipart/chunk', headers=headers,
                               data={'upload_id': rid, 'chunk_index': '0'},
                               files={'chunk': ('piece', b'abc', 'application/octet-stream')})
    assert rejected.status_code in (400, 500), rejected.text
    assert private.read_bytes() == b'previous'
    assert sorted(str(path.relative_to(foreign)) for path in foreign.rglob('*')) == before
    assert list(minio_storage._client.list_objects(minio_storage._bucket)) == []
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 0
        assert conn.scalar(text('SELECT status FROM upload_reservations WHERE id=:id'),
                           {'id': rid}) == ('cancelled' if phase == 'init' else 'active')
    if phase == 'chunk':
        original.unlink()
        retired.rename(original)
        retried = client.post('/api/files/multipart/chunk', headers=headers,
                             data={'upload_id': rid, 'chunk_index': '0'},
                             files={'chunk': ('piece', b'abc', 'application/octet-stream')})
        assert retried.status_code == 200, retried.text
        completed = client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': rid})
        assert completed.status_code == 201, completed.text
        assert client.get('/api/files/' + str(completed.json()['data']['id']) + '/download',
                          headers=headers).content == b'abc'
        assert private.read_bytes() == b'previous'


@pytest.mark.parametrize('alias', ['chunk', 'chunks', 'workspace'])
def test_multipart_alias_cannot_publish_foreign_bytes(upload_api: Any, postgres: Any, minio_storage: Any, alias: Any) -> None:
    client, headers, root = upload_api
    admitted = client.post('/api/files/multipart/init', headers=headers,
                           json={'filename': 'safe.txt', 'total_size': 6})
    assert admitted.status_code == 200
    rid = admitted.json()['data']['upload_id']
    saved = client.post('/api/files/multipart/chunk', headers=headers,
                        data={'upload_id': rid, 'chunk_index': '0'},
                        files={'chunk': ('piece', b'safe!!', 'application/octet-stream')})
    assert saved.status_code == 200
    chunks = root / 'uploads' / '.multipart' / '1' / rid / 'chunks'
    foreign = root / 'foreign'
    foreign.mkdir()
    if alias == 'chunk':
        original = chunks / '0.part'
        target = foreign / 'private.txt'
    elif alias == 'chunks':
        original = chunks
        target = foreign / 'chunks'
        target.mkdir()
    else:
        original = root / 'uploads' / '.multipart' / '1'
        target = foreign / 'workspace'
        (target / rid / 'chunks').mkdir(parents=True)
    private = target if alias == 'chunk' else target / ('0.part' if alias == 'chunks' else rid + '/chunks/0.part')
    private.write_bytes(b'secret')
    retired = original.with_name(original.name + '-retired')
    original.rename(retired)
    original.symlink_to(target, target_is_directory=alias != 'chunk')
    completed = client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': rid})
    assert completed.status_code == 400, completed.text
    assert b'secret' not in completed.content
    assert private.read_bytes() == b'secret'
    assert list(minio_storage._client.list_objects(minio_storage._bucket)) == []
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 0
        assert conn.scalar(text("SELECT status FROM upload_reservations WHERE id=:id"), {'id': rid}) == 'active'
    original.unlink()
    retired.rename(original)
    retried = client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': rid})
    assert retried.status_code == 201, retried.text
    file_id = retried.json()['data']['id']
    assert client.get(f'/api/files/{file_id}/download', headers=headers).content == b'safe!!'
    assert private.read_bytes() == b'secret'


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
        bucket = 'upload-test-' + uuid.uuid4().hex[:12]
        client = MinIOStorageClient(endpoint, access, secret, bucket)
        setattr(client, "integration_endpoint", endpoint)
        setattr(client, "integration_auth", (access, secret))
        setattr(client, "integration_bucket", bucket)
        yield client
    finally:
        docker('rm', '-f', name, check=False)


@pytest.fixture
def upload_api(postgres: Any, minio_storage: Any, monkeypatch: Any, tmp_path: Any) -> tuple[TestClient, dict[str, str], Any]:
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
    monkeypatch.setattr(storage._holder, 'client', minio_storage)
    monkeypatch.setattr(upload_config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    app = create_fastapi_app()
    def database():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_session_factory] = lambda: factory
    token = generate_token(1)
    assert token is not None
    headers = {'authorization': 'Bearer ' + token, 'X-Workspace-Id': '1'}
    # 启动门禁有单独的真实 PG 测试;
    # 此 HTTP fixture 不使用无关的全局部署连接.
    return TestClient(app), headers, tmp_path


def test_authenticated_upload_download_multipart_owner_and_limits(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any) -> None:
    client, headers, root = upload_api
    response = client.post('/api/files', headers=headers, files={'file': ('small.txt', b'hello', 'text/plain')})
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload['success'] is True
    record = payload['data']
    assert record['file_size'] == 5
    assert 'file_path' not in record and 'vector_info' not in record
    assert client.get(f"/api/files/{record['id']}/download",
                      params={'token': generate_token(1)},
                      headers={'X-Workspace-Id': '1'}).status_code == 401
    response = client.get(f"/api/files/{record['id']}/download", headers=headers)
    assert response.status_code == 200 and response.content == b'hello'
    monkeypatch.setattr(upload_config, 'MAX_UPLOAD_SIZE', 5)
    response = client.post('/api/files', headers=headers, files={'file': ('large.txt', b'too large', 'text/plain')})
    assert response.status_code == 413
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 1
    monkeypatch.setattr(upload_config, 'MAX_UPLOAD_SIZE', 1024)
    body = b'12345678'
    response = client.post('/api/files/multipart/init', headers=headers, json={
        'filename': 'multipart.txt', 'total_size': len(body), 'chunk_size': 4,
        'content_hash': hashlib.sha256(body).hexdigest()})
    assert response.status_code == 200, response.text
    multipart_payload = response.json()
    assert multipart_payload['success'] is True
    multipart_data = multipart_payload['data']
    assert 'object_name' not in multipart_data and 'stage_id' not in multipart_data
    rid = multipart_data['upload_id']
    token2 = generate_token(2)
    assert token2 is not None
    other = {**headers, 'authorization': 'Bearer ' + token2}
    assert client.get('/api/files/multipart/' + rid, headers=other).status_code == 403
    response = client.post('/api/files/multipart/chunk', headers=other,
        data={'upload_id': rid, 'chunk_index': 0}, files={'chunk': ('part', body[:4])})
    assert response.status_code == 403
    for index in range(2):
        response = client.post('/api/files/multipart/chunk', headers=headers,
            data={'upload_id': rid, 'chunk_index': index}, files={'chunk': ('part', body[index*4:(index+1)*4])})
        assert response.status_code == 200, response.text
    status = client.get('/api/files/multipart/' + rid, headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()['success'] is True
    assert status.json()['data'] == {
        'upload_id': rid,
        'chunk_size': 4,
        'total_chunks': 2,
        'uploaded_chunks': [0, 1],
    }
    response = client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': rid})
    assert response.status_code == 201, response.text
    completed_data = response.json()['data']
    assert response.json()['success'] is True
    assert 'file_path' not in completed_data and 'vector_info' not in completed_data
    assert client.get(f"/api/files/{completed_data['id']}/download", headers=headers).content == body
    assert client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': rid}).status_code == 409
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 2
        assert conn.scalar(text('SELECT count(*) FROM file_task_outbox WHERE delivered_at IS NULL')) == 2
        assert conn.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == 2
        assert conn.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 2
    assert not (root / 'uploads' / '.staging').exists()


def test_batch_upload_http_response_filters_storage_metadata(upload_api: Any, postgres: Any, minio_storage: Any) -> None:
    client, headers, _ = upload_api
    response = client.post(
        '/api/files/batch',
        headers=headers,
        files=[
            ('files', ('first.txt', b'first')),
            ('files', ('second.txt', b'second')),
        ],
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload['success'] is True
    records = payload['data']
    assert [item['name'] for item in records] == ['first.txt', 'second.txt']
    assert [item['file_size'] for item in records] == [5, 6]
    assert all('file_path' not in item and 'vector_info' not in item for item in records)
    for item, body in zip(records, [b'first', b'second']):
        downloaded = client.get(
            f"/api/files/{item['id']}/download", headers=headers,
        )
        assert downloaded.status_code == 200
        assert downloaded.content == body
    with postgres.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM files')) == 2
        assert db.scalar(text('SELECT count(*) FROM file_task_outbox')) == 2
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 2


def test_worker_reconciles_real_orphan_object_and_retains_committed_object(upload_api: Any, postgres: Any, minio_storage: Any) -> None:
    client, headers, _root = upload_api
    response = client.post('/api/files', headers=headers, files={'file': ('keep.txt', b'keep')})
    assert response.status_code == 201, response.text
    minio_storage.upload_data(b'orphan', 'crash-orphan')
    with Session(postgres) as session:
        row = upload_quota.reserve(session, upload_quota.ReserveParams(workspace_id=1, user_id=1, size=6, details={'object_name': 'crash-orphan'}))
        rid = row.id
        upload_quota.cancel(session, upload_quota.CancelParams(workspace_id=1, user_id=1, reservation_id=rid))
        service.cleanup_expired_uploads(session=session)
        reservation = session.get(UploadReservation, rid)
        assert reservation is not None and reservation.cleaned_at is not None
    assert not minio_storage.file_exists('crash-orphan')
    assert client.get(f"/api/files/{response.json()['data']['id']}/download", headers=headers).content == b'keep'
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 1


@pytest.mark.parametrize('link_level', ['root', 'workspace'])
@pytest.mark.parametrize('operation', ['worker', 'abort'])
def test_closed_reservation_cleanup_does_not_follow_directory_aliases(upload_api: Any, postgres: Any, link_level: Any, operation: Any) -> None:
    client, headers, root = upload_api
    outside = root / 'outside'
    upload_id = 'closed-upload-123'
    directory = outside / '1' / upload_id
    directory.mkdir(parents=True)
    payload = directory / 'part'
    payload.write_bytes(b'outside-reservation-tree')
    multipart = root / 'uploads' / '.multipart'
    multipart.parent.mkdir(parents=True, exist_ok=True)
    if link_level == 'root':
        multipart.symlink_to(outside, target_is_directory=True)
    else:
        multipart.mkdir()
        (multipart / '1').symlink_to(outside / '1', target_is_directory=True)
    with postgres.begin() as db:
        db.execute(text("""INSERT INTO upload_reservations(id,user_id,workspace_id,size,status,kind,details,created_at,expires_at)
            VALUES (:id,1,1,10,'cancelled','multipart','{}',now()-interval '2 hours',now()-interval '1 hour')"""), {'id': upload_id})
    if operation == 'worker':
        with sessionmaker(bind=postgres)() as db:
            assert service.cleanup_expired_uploads(session=db) == 0
    else:
        aborted = client.delete('/api/files/multipart/' + upload_id, headers=headers)
        assert aborted.status_code == 204 and aborted.content == b''
    assert payload.read_bytes() == b'outside-reservation-tree'


@pytest.mark.parametrize('endpoint', ['preflight', 'multipart/init'])
@pytest.mark.parametrize('local_source', [False, True])
def test_instant_upload_never_reuses_foreign_workspace_record(upload_api: Any, postgres: Any, minio_storage: Any, endpoint: Any, local_source: Any) -> None:
    client, headers, _ = upload_api
    content = b'guessed-private-content'
    content_hash = hashlib.sha256(content).hexdigest()
    minio_storage.upload_data(content, 'foreign-private-object')
    with postgres.begin() as db:
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (2,'foreign-private',2)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (2,2,'admin')"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,description,content_hash,workspace_id,uploader_id) VALUES (500,'private-name','foreign-private-object',:size,'success','private-derived-note',:hash,2,2)"), {'size': len(content), 'hash': content_hash})
        if local_source:
            db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,description,content_hash,workspace_id,uploader_id) VALUES (501,'allowed-name','allowed-object',:size,'success','allowed-derived-note',:hash,1,1)"), {'size': len(content), 'hash': content_hash})
    if local_source:
        minio_storage.upload_data(content, 'allowed-object')
    response = client.post('/api/files/' + endpoint, headers=headers, json={
        'filename': 'claimed.txt', 'total_size': len(content), 'chunk_size': len(content),
        'content_hash': content_hash})
    assert response.status_code == 200, response.text
    response_payload = response.json()
    assert response_payload['success'] is True
    payload = response_payload['data']
    assert payload['instant_upload'] is local_source
    assert 'private-derived-note' not in response.text and 'private-name' not in response.text
    with postgres.connect() as db:
        created = db.execute(text("SELECT file_path,description FROM files WHERE name='claimed.txt'")).all()
        assert created == ([('allowed-object', 'allowed-derived-note')] if local_source else [])
        assert db.scalar(text('SELECT count(*) FROM files WHERE workspace_id=2')) == 1
    if local_source:
        assert client.get(f"/api/files/{payload['file']['id']}/download", headers=headers).content == content
    elif endpoint == 'multipart/init':
        assert payload['upload_id'] and payload['uploaded_chunks'] == []
        assert client.delete('/api/files/multipart/' + payload['upload_id'], headers=headers).status_code == 204


@pytest.mark.parametrize('endpoint', ['single', 'batch', 'preflight', 'multipart/init'])
def test_upload_publication_rechecks_parent_after_reservation_commit(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any, endpoint: Any) -> None:
    from app.features.file import upload_authorization
    client, headers, _ = upload_api
    content = b'parent-scope-test'
    content_hash = hashlib.sha256(content).hexdigest()
    with postgres.begin() as db:
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (2,'foreign',2)"))
        db.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (10,'destination',1,1)"))
        db.execute(text("INSERT INTO files(name,file_path,file_size,status,content_hash,workspace_id,uploader_id,parent_id) VALUES ('source','allowed-source',:size,'success',:hash,1,1,1)"), {'size': len(content), 'hash': content_hash})
    minio_storage.upload_data(content, 'allowed-source')
    reserve = upload_authorization.reserve
    def move_parent_after_commit(*args: Any, **kwargs: Any):
        reservation = reserve(*args, **kwargs)
        with postgres.begin() as db:
            db.execute(text('UPDATE folder SET workspace_id=2 WHERE id=10'))
        return reservation
    monkeypatch.setattr(upload_authorization, 'reserve', move_parent_after_commit)
    if endpoint in ('single', 'batch'):
        response = client.post('/api/files' + ('/batch' if endpoint == 'batch' else ''),
            headers=headers, data={'parent_id': '10'}, files={
                'files' if endpoint == 'batch' else 'file': ('claimed.txt', content, 'text/plain')})
    else:
        response = client.post('/api/files/' + endpoint, headers=headers, json={
            'filename': 'claimed.txt', 'total_size': len(content), 'chunk_size': len(content),
            'content_hash': content_hash, 'parent_id': 10})
    assert response.status_code == 403, response.text
    with postgres.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM files WHERE name='claimed.txt'")) == 0
        assert db.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
        assert db.scalar(text('SELECT workspace_id FROM folder WHERE id=10')) == 2
        assert db.scalar(text("SELECT count(*) FROM files WHERE file_path='allowed-source'")) == 1
    assert [obj.object_name for obj in minio_storage._client.list_objects(minio_storage._bucket)] == ['allowed-source']


@pytest.mark.parametrize('body,status', [(b'ab', 400), (b'abcd', 413)])
def test_chunk_cleanup_failure_preserves_public_validation_and_retry(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any, body: Any, status: Any) -> None:
    from app.infra import multipart_storage
    from loguru import logger
    client, headers, root = upload_api
    response = client.post('/api/files/multipart/init', headers=headers, json={
        'filename': 'cleanup.txt', 'total_size': 3, 'chunk_size': 3})
    assert response.status_code == 200, response.text
    upload_id = response.json()['data']['upload_id']
    data = {'upload_id': upload_id, 'chunk_index': 0}
    path = '/api/files/multipart/chunk'
    assert client.post(path, headers=headers, data=data, files={'chunk': ('part', b'abc')}).status_code == 200
    original_unlink = multipart_storage.os.unlink
    logs: list[str] = []
    def log_sink(message: Any) -> None:
        logs.append(str(message))
    sink = logger.add(log_sink)
    def reject_temporary(path: Any, **kwargs: Any):
        if str(path).endswith('.tmp'):
            raise PermissionError('private-cleanup-path-and-credential')
        return original_unlink(path, **kwargs)
    try:
        monkeypatch.setattr(multipart_storage.os, 'unlink', reject_temporary)
        failed = client.post(path, headers=headers, data=data, files={'chunk': ('part', body)})
    finally:
        monkeypatch.setattr(multipart_storage.os, 'unlink', original_unlink)
        logger.remove(sink)
    assert failed.status_code == status, failed.text
    assert failed.json()['error']['code'] == ('BUSINESS_RULE_VIOLATION' if status == 400 else 'PAYLOAD_TOO_LARGE')
    assert 'private-cleanup' not in failed.text + ''.join(logs)
    assert 'PermissionError' in ''.join(logs)
    chunks = root / 'uploads' / '.multipart' / '1' / upload_id / 'chunks'
    assert (chunks / '0.part').read_bytes() == b'abc'
    assert list(chunks.glob('*.tmp'))
    assert client.post(path, headers=headers, data=data, files={'chunk': ('part', b'xyz')}).status_code == 200
    complete = client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': upload_id})
    assert complete.status_code == 201, complete.text
    assert client.get(f"/api/files/{complete.json()['data']['id']}/download", headers=headers).content == b'xyz'
    assert not chunks.exists()
    with postgres.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 1


@pytest.mark.parametrize('kind', ['single', 'batch', 'chunk', 'avatar'])
async def test_cancelled_request_preserves_real_upload_publication(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any, kind: Any) -> None:
    client, headers, root = upload_api
    entered, release = threading.Event(), threading.Event()
    streams: list[Any] = []
    consumed: list[bytes] = []
    original_save = FastAPIUploadAdapter.save

    def held_save(adapter: Any, destination: Any, *args: Any, **kwargs: Any):
        streams.append(adapter.upload_file.file)
        entered.set()
        assert release.wait(5)
        original_save(adapter, destination, *args, **kwargs)
        consumed.append(Path(destination).read_bytes())

    monkeypatch.setattr(FastAPIUploadAdapter, 'save', held_save)
    if kind == 'chunk':
        from app.infra import multipart_storage
        original_copy = multipart_storage.copy_upload_bounded
        def held_copy(adapter: Any, target: Any, max_bytes: Any):
            streams.append(adapter.upload_file.file)
            entered.set()
            assert release.wait(5)
            original_copy(adapter, target, max_bytes)
            adapter.upload_file.file.seek(0)
            consumed.append(adapter.upload_file.file.read())
        monkeypatch.setattr(multipart_storage, 'copy_upload_bounded', held_copy)
    path, field = {
        'single': ('/api/files', 'file'),
        'batch': ('/api/files/batch', 'files'),
        'chunk': ('/api/files/multipart/chunk', 'chunk'),
        'avatar': ('/api/files/upload/avatar/1', 'avatar'),
    }[kind]
    bodies = [b'owned-upload-0', b'owned-upload-1'] if kind == 'batch' else [b'owned-upload-0']
    data = {}
    upload_id: str | None = None
    if kind == 'chunk':
        response = client.post('/api/files/multipart/init', headers=headers, json={
            'filename': 'owned.txt', 'total_size': len(bodies[0]), 'chunk_size': len(bodies[0]),
            'content_hash': hashlib.sha256(bodies[0]).hexdigest()})
        assert response.status_code == 200, response.text
        upload_id = response.json()['data']['upload_id']
        data = {'upload_id': upload_id, 'chunk_index': '0'}
    files = [(field, (f'owned-{index}.txt', body, 'text/plain')) for index, body in enumerate(bodies)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://test') as requester:
        pending = asyncio.create_task(requester.post(path, headers=headers, files=files, data=data))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            pending.cancel('request-disconnected')
            await asyncio.sleep(.01)
            pending.cancel('repeat-disconnect')
            await asyncio.sleep(.01)
            assert not pending.done() and not consumed
            assert all(not getattr(stream, 'closed', False) for stream in streams)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending, 5)
    assert str(error.value) == 'request-disconnected'
    assert consumed == bodies and all(getattr(stream, 'closed', False) for stream in streams)
    if kind == 'chunk':
        assert upload_id is not None
        response = client.post('/api/files/multipart/complete', headers=headers, json={'upload_id': upload_id})
        assert response.status_code == 201, response.text
    with postgres.connect() as conn:
        file_ids = list(conn.scalars(text('SELECT id FROM files ORDER BY id')))
        assert len(file_ids) == len(bodies)
        assert conn.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
        assert conn.scalar(text('SELECT count(*) FROM file_task_outbox')) == len(bodies)
        if kind == 'avatar':
            assert conn.scalar(text('SELECT avatar FROM users WHERE id=1')).startswith('/api/share/')
    for file_id, body in zip(file_ids, bodies):
        assert client.get(f'/api/files/{file_id}/download', headers=headers).content == body
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == len(bodies)
    assert not (root / 'uploads' / '.staging').exists()


def test_real_database_commit_failure_removes_uploaded_object(upload_api: Any, postgres: Any, minio_storage: Any) -> None:
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


def test_real_storage_rejection_holds_capacity_until_reconciliation(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any) -> None:
    from minio import Minio
    from app.exceptions import BusinessRuleError
    client, headers, root = upload_api
    original_client = minio_storage._client
    # 执行真实的 S3 认证失败 (包括清理失败),
    # 而不使用 Mock 替换任何存储操作.
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
            upload_quota.reserve(session, upload_quota.ReserveParams(workspace_id=1, user_id=1, size=1))
        session.rollback()
    minio_storage._client = original_client
    with Session(postgres) as session:
        service.cleanup_expired_uploads(session=session)
        assert session.query(UploadReservation).one().cleaned_at is not None
        assert upload_quota.reserve(session, upload_quota.ReserveParams(workspace_id=1, user_id=1, size=5)).size == 5
    assert list(original_client.list_objects(minio_storage._bucket)) == []
    assert not (root / 'uploads' / '.staging').exists()


def test_process_crash_after_real_put_is_reconciled(upload_api: Any, postgres: Any, minio_storage: Any) -> None:
    from datetime import timedelta
    _, _, root = upload_api
    script = """
import os
from io import BytesIO
saved_environment = dict(os.environ)
import tests.conftest  # Only optional AI/Docker libraries are stubbed.
from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.features.file import upload_config, upload_service
from app.features.file.upload_service import FileUploadService
from app.infra.storage import MinIOStorageClient
from app.infra.upload_adapter import FastAPIUploadAdapter
engine = create_engine(saved_environment['DATABASE_URL'])
upload_config.UPLOAD_FOLDER = saved_environment['UPLOAD_FOLDER']
storage = MinIOStorageClient(saved_environment['MINIO_ENDPOINT'], saved_environment['MINIO_ACCESS_KEY'],
    saved_environment['MINIO_SECRET_KEY'], saved_environment['MINIO_BUCKET'])
uploads = FileUploadService(upload_config.upload_settings(), storage_factory=lambda: storage)
upload_service.stage_stored_files = lambda *args, **kwargs: os._exit(31)
with Session(engine) as session:
    uploads.create_uploaded_file(
        session, 1, 1,
        FastAPIUploadAdapter(UploadFile(filename='crash.txt', file=BytesIO(b'crashed upload'))),
    )
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
        reservation = session.get(UploadReservation, rid)
        assert reservation is not None
        assert reservation.status == 'expired'
        assert reservation.cleaned_at is not None
        assert not stage.exists()
        assert not minio_storage.file_exists(object_name)
    with postgres.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM files')) == 0


@pytest.mark.parametrize('delete_failure', [False, True])
def test_real_batch_staging_failure_compensation_and_worker_recovery(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any, delete_failure: Any) -> None:
    """输入流失败补偿先前的对象写入与配额."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.exceptions import BusinessRuleError, ServiceOperationError
    from app.features.file.upload_service import get_upload_service
    from app.infra.upload_adapter import FastAPIUploadAdapter
    from minio import Minio

    _, _, root = upload_api
    monkeypatch.setattr(upload_quota, 'USER_CAPACITY', 10)
    healthy_client = minio_storage._client
    original_save = FastAPIUploadAdapter.save
    calls = 0

    def fail_second(adapter: Any, destination: Any, max_bytes: Any):
        nonlocal calls
        calls += 1
        if calls == 2:
            if delete_failure:
                minio_storage._client = Minio(
                    minio_storage.integration_endpoint,
                    access_key='invalid',
                    secret_key='invalid-secret',
                    secure=False,
                )
            original_save(adapter, destination, max_bytes)
            raise OSError('private-staging-allocation-fault')
        original_save(adapter, destination, max_bytes)

    monkeypatch.setattr(FastAPIUploadAdapter, 'save', fail_second)
    uploads = [
        FastAPIUploadAdapter(UploadFile(filename=name, file=BytesIO(b'12345')))
        for name in ['one.txt', 'two.txt']
    ]
    try:
        with Session(postgres) as db:
            with pytest.raises(ServiceOperationError, match='Failed to upload file'):
                get_upload_service().create_uploaded_files(db, 1, 1, cast(list[Any], uploads))
            assert db.scalar(text('SELECT count(*) FROM files')) == 0
            assert db.scalar(text('SELECT count(*) FROM file_task_outbox')) == 0
            assert db.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0
            assert db.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == 1
            if delete_failure:
                with pytest.raises(BusinessRuleError, match='capacity'):
                    upload_quota.reserve(
                        db,
                        upload_quota.ReserveParams(
                            workspace_id=1, user_id=1, size=10,
                        ),
                    )
                db.rollback()
            minio_storage._client = healthy_client
            assert len(list(healthy_client.list_objects(minio_storage._bucket))) == int(delete_failure)
            service.cleanup_expired_uploads(session=db)
            assert db.scalar(text('SELECT count(*) FROM upload_reservations WHERE cleaned_at IS NULL')) == 0
            assert upload_quota.reserve(
                db,
                upload_quota.ReserveParams(workspace_id=1, user_id=1, size=10),
            ).size == 10
        assert list(healthy_client.list_objects(minio_storage._bucket)) == []
        assert not (root / 'uploads' / '.staging').exists()
    finally:
        minio_storage._client = healthy_client

@pytest.mark.parametrize('kind', ['single', 'batch', 'multipart'])
def test_real_upload_committed_before_ack_failure_keeps_downloadable_objects(upload_api: Any, postgres: Any, minio_storage: Any, kind: Any) -> None:
    """已提交的发布在每个上传接缝丢失确认时仍幸存."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.exceptions import ServiceOperationError
    from app.models.file import File
    from app.features.file.multipart_service import SaveMultipartChunkParams, get_multipart_service
    from app.features.file.upload_service import get_upload_service
    from app.infra.upload_adapter import FastAPIUploadAdapter

    class AckSession(Session):
        def commit(self):
            publication_committed = any(
                isinstance(row, File)
                for row in (*self.new, *self.identity_map.values())
            )
            super().commit()
            if publication_committed and not self.info.get('upload_ack_dropped', False):
                self.info['upload_ack_dropped'] = True
                raise OSError('private-commit-acknowledgement-fault')
    client, headers, root = upload_api

    def upload(body: Any):
        return FastAPIUploadAdapter(UploadFile(filename='x.txt', file=BytesIO(body)))

    with AckSession(postgres) as db:
        with pytest.raises(ServiceOperationError):
            if kind == 'single':
                get_upload_service().create_uploaded_file(db, 1, 1, upload(b'12345'))
            elif kind == 'batch':
                get_upload_service().create_uploaded_files(
                    db, 1, 1, [upload(b'12345'), upload(b'67890')],
                )
            else:
                multipart = get_multipart_service()
                rid = str(multipart.init_multipart_upload(
                    db, 1, 1, {'filename': 'x.txt', 'total_size': 5},
                )['upload_id'])
                multipart.save_multipart_chunk(
                    db,
                    SaveMultipartChunkParams(
                        uploader_id=1,
                        workspace_id=1,
                        upload_id=rid,
                        chunk_index=0,
                        chunk=upload(b'12345'),
                    ),
                )
                multipart.complete_multipart_upload(db, 1, 1, rid)

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
        assert response.content in [b'12345', b'67890']
    assert not (root / 'uploads' / '.staging').exists()


@pytest.mark.parametrize('rollback_fault', [False, True])
def test_real_batch_cancel_sql_rejection_preserves_error_and_recovers_remaining_entries(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any, rollback_fault: Any) -> None:
    """PostgreSQL 取消拒绝保留输入错误与恢复状态."""
    from io import BytesIO
    from fastapi import UploadFile
    from app.exceptions import BusinessRuleError
    from types import SimpleNamespace
    from app.infra import upload_staging
    from app.features.file.upload_service import get_upload_service
    from app.infra.upload_adapter import FastAPIUploadAdapter

    _, _, root = upload_api
    monkeypatch.setattr(upload_quota, 'USER_CAPACITY', 10)
    original_save = FastAPIUploadAdapter.save
    original = BusinessRuleError('original upload failure')
    calls = 0
    first_id = 'a' * 32
    with postgres.begin() as sql:
        sql.execute(text("ALTER TABLE upload_reservations ADD CONSTRAINT reject_first_cancel CHECK (id <> 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' OR status <> 'cancelled')"))
    stage_ids = iter((first_id, 'b' * 32, 'c' * 32))
    monkeypatch.setattr(
        upload_staging,
        'uuid',
        SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(stage_ids))),
    )

    def save_then_fail(adapter: Any, destination: Any, max_bytes: Any):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise original
        original_save(adapter, destination, max_bytes)

    monkeypatch.setattr(FastAPIUploadAdapter, 'save', save_then_fail)
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
        uploads = [
            FastAPIUploadAdapter(
                UploadFile(filename='x.txt', file=BytesIO(b'12345')),
            )
            for _ in range(3)
        ]
        with pytest.raises(BusinessRuleError) as caught:
            get_upload_service().create_uploaded_files(db, 1, 1, cast(list[Any], uploads))
        assert caught.value is original
    with Session(postgres) as observer:
        rows = observer.execute(text('SELECT id,status FROM upload_reservations')).all()
        assert len(rows) == 2
        assert {row.status for row in rows} == {'active', 'cancelled'}
        assert next(row for row in rows if row.id == first_id).status == 'active'
        assert observer.scalar(text('SELECT count(*) FROM files')) == 0
        assert observer.scalar(text('SELECT sum(attempts) FROM upload_daily_usage')) == 2
    assert list(minio_storage._client.list_objects(minio_storage._bucket)) == []
    with postgres.begin() as sql:
        sql.execute(text('ALTER TABLE upload_reservations DROP CONSTRAINT reject_first_cancel'))
        sql.execute(
            text("UPDATE upload_reservations SET expires_at=NOW()-INTERVAL '1 second' WHERE id=:id"),
            {'id': first_id},
        )
    with Session(postgres) as observer:
        service.cleanup_expired_uploads(session=observer)
        assert observer.scalar(text('SELECT count(*) FROM upload_reservations WHERE cleaned_at IS NULL')) == 0
        assert upload_quota.reserve(
            observer,
            upload_quota.ReserveParams(workspace_id=1, user_id=1, size=10),
        ).size == 10
    assert not (root / 'uploads' / '.staging').exists()


@pytest.mark.parametrize('format', ['multipart','form','json'])
def test_real_avatar_upload_formats_update_account_and_keep_share_bytes(upload_api: Any, postgres: Any, minio_storage: Any, format: Any) -> None:
    client, headers, _root = upload_api
    import base64
    body = b'avatar-bytes'
    value = 'data:image/png;base64,' + base64.b64encode(body).decode()
    kwargs = {'files':{'avatar':('avatar.png',body,'image/png')}} if format == 'multipart' else ({'data':{'avatar_base64':value}} if format == 'form' else {'json':{'avatar':value}})
    response = client.post('/api/files/upload/avatar/1',headers=headers,**kwargs)
    assert response.status_code == 200,response.text
    payload = response.json()
    assert payload['success'] is True
    data = payload['data']
    assert 'file_path' not in data and 'vector_info' not in data
    assert data['avatar'] == data['avatar_url']
    with postgres.connect() as sql:
        assert sql.scalar(text('SELECT avatar FROM users WHERE id=1')) == data['avatar_url']
        assert sql.scalar(text('SELECT count(*) FROM files')) == 1
    shared = client.get(data['avatar_url'])
    assert shared.status_code == 200 and shared.content == body
    assert len(list(minio_storage._client.list_objects(minio_storage._bucket))) == 1


def test_real_avatar_worker_rejects_stale_admin_role_before_object_put(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any) -> None:
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.dependencies import get_current_user
    from app.exceptions import register_exception_handlers
    from app.features.file import router
    _client, _headers, _root = upload_api
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
def test_real_avatar_publication_sql_failure_and_commit_ack_outcome(upload_api: Any,postgres: Any,minio_storage: Any,failure: Any) -> None:
    client,headers,root=upload_api
    with postgres.begin() as sql:
        sql.execute(text("UPDATE users SET avatar='/existing-avatar' WHERE id=1"))
        if failure=='share':
            sql.execute(text('ALTER TABLE shares ADD CONSTRAINT reject_avatar_share CHECK (file_id < 0)'))
        elif failure=='profile':
            sql.execute(text("ALTER TABLE users ADD CONSTRAINT reject_avatar_url CHECK (avatar NOT LIKE '/api/share/%')"))
    if failure == 'ack':
        from app.models.file import File

        class AckSession(Session):
            def commit(self):
                publication_committed = any(
                    isinstance(row, File)
                    for row in (*self.new, *self.identity_map.values())
                )
                super().commit()
                if publication_committed and not self.info.get('avatar_ack_dropped', False):
                    self.info['avatar_ack_dropped'] = True
                    raise OSError('private-avatar-commit-ack-fault')

        client.app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres,class_=AckSession)
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
def test_real_upload_cleanup_close_fault_does_not_replace_sql_outcome(upload_api: Any,postgres: Any,minio_storage: Any,monkeypatch: Any,metadata_rejection: Any) -> None:
    client,headers,root=upload_api
    if metadata_rejection:
        with postgres.begin() as sql:sql.execute(text("ALTER TABLE files ADD CONSTRAINT reject_close_test CHECK (name <> 'close.txt')"))
    from app.infra.upload_staging import StagedUpload
    close = StagedUpload.close
    closed: list[bool] = []
    def fault(stage: Any):
        close(stage)
        closed.append(stage.handle.closed)
        raise OSError('private-close-fault')
    monkeypatch.setattr(StagedUpload, 'close', fault)
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
def test_upload_publication_rejects_committed_role_loss(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any, kind: Any) -> None:
    """真实 SQL/对象字节数据; 显式边界钩子提交角色丢失."""
    client, headers, _root = upload_api
    baseline_files = baseline_tasks = baseline_objects = 0
    source = None
    if kind == 'instant':
        source = client.post('/api/files', headers=headers, files={'file': ('source.txt', b'abc')})
        assert source.status_code == 201, source.text
        with postgres.connect() as db:
            baseline_files = db.scalar(text('SELECT count(*) FROM files'))
            baseline_tasks = db.scalar(text('SELECT count(*) FROM file_task_outbox'))
        baseline_objects = len(list(minio_storage._client.list_objects(minio_storage._bucket)))
        reserve = upload_quota.reserve
        def reserve_then_demote(*args: Any, **kwargs: Any):
            row = reserve(*args, **kwargs)
            with postgres.begin() as independent:
                independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
            return row
        monkeypatch.setattr(upload_quota, 'reserve', reserve_then_demote)
    else:
        put = minio_storage.upload_file
        puts = 0
        def put_then_demote(*args: Any, **kwargs: Any):
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
        # HTTP 入口在完成前已持有成员身份.
        # 直接执行共享服务边界而无需额外请求锁.
        from app.exceptions import PermissionDeniedError
        from app.features.file.multipart_service import get_multipart_service
        with Session(postgres) as db:
            with pytest.raises(PermissionDeniedError):
                get_multipart_service().complete_multipart_upload(db, 1, 1, upload_id)
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
        assert source is not None
        assert client.get('/api/files/' + str(source.json()['data']['id']) + '/download', headers=headers).content == b'abc'


@pytest.mark.parametrize('format', ['multipart', 'form', 'json'])
@pytest.mark.parametrize('loss', ['demote', 'delete'])
def test_admin_avatar_publication_rechecks_acting_identity(upload_api: Any, postgres: Any, minio_storage: Any, monkeypatch: Any, format: Any, loss: Any) -> None:
    import base64
    client, headers, _root = upload_api
    with postgres.begin() as db:
        db.execute(text("UPDATE users SET role='admin' WHERE id=1"))
        db.execute(text("UPDATE users SET avatar='/existing-avatar' WHERE id=2"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (2,'target-private',2)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (2,2,'admin')"))
        db.execute(text("INSERT INTO folder(name,workspace_id,parent_id) VALUES ('/',2,NULL)"))
    put = minio_storage.upload_file
    def lose_identity(*args: Any, **kwargs: Any):
        result = put(*args, **kwargs)
        with postgres.begin() as independent:
            independent.execute(text('DELETE FROM users WHERE id=1' if loss == 'delete'
                else "UPDATE users SET role='common' WHERE id=1"))
        return result
    monkeypatch.setattr(minio_storage, 'upload_file', lose_identity)
    encoded = 'data:image/png;base64,' + base64.b64encode(b'avatar-image').decode()
    if format == 'multipart':
        response = client.post('/api/files/upload/avatar/2', headers=headers,
                               files={'avatar': ('avatar.png', b'avatar-image', 'image/png')})
    elif format == 'form':
        response = client.post('/api/files/upload/avatar/2', headers=headers, data={'avatar_base64': encoded})
    else:
        response = client.post('/api/files/upload/avatar/2', headers=headers, json={'avatar': encoded})
    assert response.status_code == 403, response.text
    with postgres.connect() as db:
        assert db.scalar(text('SELECT avatar FROM users WHERE id=2')) == '/existing-avatar'
        assert db.scalar(text('SELECT count(*) FROM files')) == 0
        assert db.scalar(text('SELECT count(*) FROM shares')) == 0
        assert db.scalar(text('SELECT count(*) FROM file_task_outbox')) == 0
        assert db.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='cancelled' AND user_id=2")) == 1
        assert db.scalar(text('SELECT sum(attempts) FROM upload_daily_usage WHERE user_id=2')) == 1
    assert not list(minio_storage._client.list_objects(minio_storage._bucket))
