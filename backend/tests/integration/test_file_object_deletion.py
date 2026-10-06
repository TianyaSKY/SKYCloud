"""Opt-in PostgreSQL/MinIO deletion atomicity, retry and clone races."""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.exceptions import ConflictError
from app.features.file import service
from app.infra.file_object_deletion import delete_pending_objects
from app.models.file import File
from entry.migrate import upgrade
from tests.integration.test_migrations import postgres
from tests.integration.test_upload_storage import minio_storage

pytestmark = pytest.mark.skipif(os.getenv('RUN_OBJECT_DELETION_TESTS') != '1',
                               reason='Opt-in isolated PostgreSQL/MinIO object deletion')


@pytest.fixture
def object_context(postgres,minio_storage,monkeypatch,tmp_path):
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'owner','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
        db.execute(text("INSERT INTO files(name,file_path,file_size,status,content_hash,workspace_id,uploader_id) VALUES ('one','shared',3,'success',:hash,1,1)"),{'hash':hashlib.sha256(b'abc').hexdigest()})
    payload = tmp_path/'payload'
    payload.write_bytes(b'abc')
    minio_storage.upload_file(str(payload),'shared')
    monkeypatch.setattr(service,'get_storage_client',lambda:minio_storage)
    monkeypatch.setattr(service.change_log_service,'log_event',lambda **kwargs:None)
    factory = sessionmaker(bind=postgres)
    return postgres,minio_storage,factory


def clean(context,**kwargs):
    _,storage,factory = context
    return delete_pending_objects(session_factory=factory,storage_factory=lambda:storage,**kwargs)


def delete_source(context):
    with context[2]() as session:
        service.delete_file(session,1)


def test_metadata_commit_failure_preserves_real_object_and_rolls_back_intent(object_context):
    db,storage,factory = object_context
    with db.begin() as connection:
        connection.execute(text("""CREATE FUNCTION reject_file_delete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected delete commit failure'; END $$"""))
        connection.execute(text("""CREATE CONSTRAINT TRIGGER reject_file_delete AFTER DELETE ON files
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_file_delete()"""))
    with factory() as session:
        with pytest.raises(Exception,match='injected delete commit failure'):
            service.delete_file(session,1)
        session.rollback()
    assert storage.file_exists('shared')
    stream = storage.get_file_stream('shared')
    try:
        assert stream.read() == b'abc'
    finally:
        storage.close_file_stream(stream)
    with db.connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM files')) == 1
        assert connection.scalar(text('SELECT count(*) FROM file_object_deletions')) == 0


def test_shared_object_survives_until_last_committed_reference(object_context):
    db,storage,factory = object_context
    with factory() as session:
        session.add(File(name='two',file_path='shared',file_size=3,status='success',workspace_id=1,uploader_id=1))
        session.commit()
    delete_source(object_context)
    assert clean(object_context) == 1 and storage.file_exists('shared')
    with factory() as session:
        service.delete_file(session,2)
    assert storage.file_exists('shared')
    assert clean(object_context) == 1 and not storage.file_exists('shared')


def test_storage_failure_is_persisted_then_actual_delete_retries(object_context,monkeypatch):
    db,storage,_ = object_context
    delete_source(object_context)
    original = storage.delete_file
    def reject(name):
        raise RuntimeError('injected storage rejection')
    monkeypatch.setattr(storage,'delete_file',reject)
    assert clean(object_context) == 0 and storage.file_exists('shared')
    with db.begin() as connection:
        row = connection.execute(text('SELECT attempts,last_error_type,completed_at FROM file_object_deletions')).one()
        assert tuple(row) == (1,'RuntimeError',None)
        connection.execute(text('UPDATE file_object_deletions SET next_attempt_at=now()'))
    monkeypatch.setattr(storage,'delete_file',original)
    assert clean(object_context) == 1 and not storage.file_exists('shared')


def test_post_delete_sql_failure_replays_idempotently(object_context):
    db,storage,_ = object_context
    delete_source(object_context)
    with db.begin() as connection:
        connection.execute(text("""CREATE FUNCTION reject_cleanup_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected cleanup commit failure'; END $$"""))
        connection.execute(text("""CREATE CONSTRAINT TRIGGER reject_cleanup AFTER UPDATE ON file_object_deletions
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_cleanup_commit()"""))
    with pytest.raises(Exception,match='injected cleanup commit failure'):
        clean(object_context)
    assert not storage.file_exists('shared')
    with db.begin() as connection:
        assert connection.scalar(text('SELECT completed_at FROM file_object_deletions')) is None
        connection.execute(text('DROP TRIGGER reject_cleanup ON file_object_deletions'))
    assert clean(object_context) == 1


def test_two_dispatchers_complete_duplicate_intents_without_losing_state(object_context):
    db,storage,_ = object_context
    delete_source(object_context)
    with db.begin() as connection:
        connection.execute(text("INSERT INTO file_object_deletions(object_name) VALUES ('shared')"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(clean,object_context,limit=1) for _ in range(2)]
        assert sum(task.result(timeout=10) for task in tasks) == 2
    assert not storage.file_exists('shared')
    with db.connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM file_object_deletions WHERE completed_at IS NOT NULL')) == 2


def test_stale_instant_source_cannot_recreate_missing_object_metadata(object_context):
    db,storage,factory = object_context
    with factory() as session:
        source = session.get(File,1)
        digest = source.content_hash
        delete_source(object_context)
        assert clean(object_context) == 1
        with pytest.raises(ConflictError,match='no longer available'):
            service._clone_existing_file(session,source,filename='clone',workspace_id=1,uploader_id=1,
                parent_id=None,mime_type=None,content_hash=digest)
    assert not storage.file_exists('shared')
    with db.connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM files')) == 0
        assert connection.scalar(text("SELECT count(*) FROM upload_reservations WHERE status='active'")) == 0


def test_cleaner_waits_for_clone_commit_and_retains_its_shared_object(object_context,monkeypatch):
    db,storage,factory = object_context
    checked,release = Event(),Event()
    original_exists = storage.file_exists
    def pause_clone_check(name):
        checked.set()
        assert release.wait(10), 'clone barrier timed out'
        return original_exists(name)
    monkeypatch.setattr(storage,'file_exists',pause_clone_check)
    def clone():
        with factory() as session:
            source = session.get(File,1)
            new = service._clone_existing_file(session,source,filename='clone',workspace_id=1,uploader_id=1,
                parent_id=None,mime_type=None,content_hash=source.content_hash)
            return new.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        clone_task = pool.submit(clone)
        try:
            assert checked.wait(5)
            delete_source(object_context)
            cleaner = pool.submit(clean,object_context)
            deadline = time.monotonic()+5
            waiting = False
            while time.monotonic() < deadline:
                with db.connect() as connection:
                    waiting = connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=731948214 AND NOT granted)"))
                if waiting:
                    break
                time.sleep(.02)
            assert waiting, 'cleaner must wait on the clone object lock'
        finally:
            release.set()
        new_id = clone_task.result(timeout=10)
        assert cleaner.result(timeout=10) == 1
    assert original_exists('shared')
    with db.connect() as connection:
        assert connection.execute(text('SELECT id,file_path FROM files')).all() == [(new_id,'shared')]


def test_recursive_foreign_child_refuses_deletion_without_losing_real_object(object_context):
    from app.features.folder import service as folders
    from app.exceptions import PermissionDeniedError
    db,storage,factory = object_context
    with db.begin() as connection:
        connection.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (2,'foreign',1)"))
        connection.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (1,'target',1,NULL)"))
        connection.execute(text("UPDATE files SET parent_id=1"))
        connection.execute(text("INSERT INTO files(name,file_path,file_size,status,workspace_id,uploader_id,parent_id) VALUES ('foreign','shared',3,'success',2,1,1)"))
    with factory() as session:
        with pytest.raises(PermissionDeniedError,match='another workspace'):
            folders.delete_folder(session,1)
    assert storage.file_exists('shared')
    with db.connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM files')) == 2
        assert connection.scalar(text('SELECT count(*) FROM file_object_deletions')) == 0


def test_recursive_root_lock_blocks_new_foreign_child_during_enumeration(object_context,monkeypatch):
    from app.features.folder import service as folders
    db,_,factory = object_context
    with db.begin() as connection:
        connection.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (2,'foreign',1)"))
        connection.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (1,'target',1,NULL)"))
        connection.execute(text("UPDATE files SET parent_id=1"))
    entered,release = Event(),Event()
    original = folders._delete_folder_recursive
    def pause(session,folder,events,visited=None):
        entered.set()
        assert release.wait(5)
        return original(session,folder,events,visited)
    monkeypatch.setattr(folders,'_delete_folder_recursive',pause)
    def delete():
        with factory() as session:
            folders.delete_folder(session,1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(delete)
        try:
            assert entered.wait(5)
            with pytest.raises(Exception,match='lock timeout'):
                with db.begin() as connection:
                    connection.execute(text("SET LOCAL lock_timeout='200ms'"))
                    connection.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (2,'foreign-child',2,1)"))
        finally:
            release.set()
        task.result(timeout=10)
    assert clean(object_context) == 1


@pytest.mark.parametrize('entity',['file','workspace','user'])
def test_bulk_and_database_cascades_stage_cleanup_in_same_transaction(object_context,entity):
    db,storage,_ = object_context
    statement = {'file':'DELETE FROM files WHERE id=1','workspace':'DELETE FROM workspaces WHERE id=1',
                 'user':'DELETE FROM users WHERE id=1'}[entity]
    # A rolled back cascade must leave both metadata and bytes; no intent survives.
    with db.connect() as connection:
        transaction = connection.begin()
        connection.execute(text(statement))
        assert connection.scalar(text('SELECT count(*) FROM file_object_deletions')) == 1
        transaction.rollback()
    with db.connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM files')) == 1
        assert connection.scalar(text('SELECT count(*) FROM file_object_deletions')) == 0
    with db.begin() as connection:
        connection.execute(text(statement))
    assert storage.file_exists('shared')
    with db.connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM files')) == 0
        assert connection.scalar(text('SELECT count(*) FROM file_object_deletions')) == 1
    assert clean(object_context) == 1 and not storage.file_exists('shared')


def test_workspace_service_cascade_retains_shared_foreign_object(object_context,monkeypatch):
    from app.features.workspace import service as workspaces,docker_service
    db,storage,factory = object_context
    monkeypatch.setattr(docker_service,'remove_container',lambda workspace,session:None)
    with db.begin() as connection:
        connection.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (2,'foreign','x','common')"))
        connection.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (2,'foreign',2)"))
        connection.execute(text("INSERT INTO files(name,file_path,file_size,status,workspace_id,uploader_id) VALUES ('foreign','shared',3,'success',2,2)"))
    with factory() as session:
        workspaces.delete_workspace(session,1,1)
    with db.connect() as connection:
        assert connection.execute(text('SELECT workspace_id FROM files')).scalars().all() == [2]
        assert connection.scalar(text('SELECT count(*) FROM file_object_deletions')) == 1
    assert clean(object_context) == 1 and storage.file_exists('shared')


def test_user_database_cascade_keeps_foreign_workspace_file_and_clears_uploader(object_context):
    db,storage,_ = object_context
    with db.begin() as connection:
        connection.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (2,'foreign','x','common')"))
        connection.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (2,'foreign',2)"))
        connection.execute(text("INSERT INTO files(name,file_path,file_size,status,workspace_id,uploader_id) VALUES ('collaboration','shared',3,'success',2,1)"))
        connection.execute(text('DELETE FROM users WHERE id=1'))
    with db.connect() as connection:
        assert connection.execute(text('SELECT workspace_id,uploader_id FROM files')).all() == [(2,None)]
        assert connection.scalar(text('SELECT count(*) FROM file_object_deletions')) == 1
    assert clean(object_context) == 1 and storage.file_exists('shared')
