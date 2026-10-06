"""Real transaction/broker fault and concurrency checks for indexing handoff."""
import concurrent.futures
from datetime import datetime, timedelta, timezone
import os
import threading

import pytest
from sqlalchemy import text

from app.features.file import service
from app.infra.file_task_outbox import deliver_pending_file_tasks
from app.infra.task_queue import RabbitMQTaskConsumer, FILE_PROCESS_QUEUE, publish_file_tasks as real_publish
from app.models.file_task_outbox import FileTaskOutbox
from app.models.file import File
from tests.integration.test_migrations import postgres  # noqa: F401
from tests.integration.test_worker_outcomes import file_database  # noqa: F401
from tests.integration.test_task_queue import broker  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_FILE_OUTBOX_TESTS") != "1", reason="Opt-in isolated PostgreSQL/RabbitMQ")


def drain():
    consumer = RabbitMQTaskConsumer(.01)
    messages = []
    try:
        while message := consumer.get_message(FILE_PROCESS_QUEUE):
            messages.append(int(message.body))
            consumer.acknowledge(message)
        return messages
    finally:
        consumer.close()


def test_broker_outage_retains_committed_intent_then_recovers(file_database, broker, monkeypatch):
    factory, file_id, _ = file_database
    with factory() as db:
        service.retry_embedding(db, file_id)
    real_port = os.environ["RABBITMQ_PORT"]
    monkeypatch.setenv("RABBITMQ_PORT", "1")
    assert deliver_pending_file_tasks(session_factory=factory, publisher=real_publish) == 0
    with factory() as db:
        row = db.query(FileTaskOutbox).one()
        assert row.delivered_at is None and row.attempts == 1
        assert row.last_error_type
        assert db.get(File, file_id).status == "pending"
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    monkeypatch.setenv("RABBITMQ_PORT", real_port)
    assert deliver_pending_file_tasks(session_factory=factory, publisher=real_publish) == 1
    assert drain() == [file_id]
    with factory() as db:
        row = db.query(FileTaskOutbox).one()
        assert row.delivered_at is not None and row.attempts == 2
        assert row.last_error_type is None
    assert deliver_pending_file_tasks(session_factory=factory, publisher=real_publish) == 0


def test_task_insert_failure_rolls_back_file_metadata(file_database, postgres):
    factory, _, workspace_id = file_database
    with postgres.begin() as db:
        db.execute(text("ALTER TABLE file_task_outbox ADD CONSTRAINT reject_intent CHECK (file_id < 0)"))
    with factory() as db:
        with pytest.raises(Exception):
            service._persist_file_record(db, name="must-rollback.txt", file_path="unused",
                file_size=5, mime_type="text/plain", workspace_id=workspace_id,
                uploader_id=None, parent_id=None, content_hash=None)
        db.rollback()
    with factory() as db:
        assert db.query(File).filter_by(name="must-rollback.txt").count() == 0
        assert db.query(FileTaskOutbox).count() == 0


def test_concurrent_dispatchers_do_not_send_a_committed_row_twice(file_database, broker):
    factory, file_id, _ = file_database
    with factory() as db:
        for _ in range(4):
            service.retry_embedding(db, file_id)
    barrier = threading.Barrier(2)
    local = threading.local()
    def overlap_publish(file_ids):
        if not getattr(local, "started", False):
            local.started = True
            # Both dispatchers must hold different rows concurrently. Without
            # SKIP LOCKED, the second dispatcher cannot reach this barrier.
            barrier.wait(timeout=5)
        real_publish(file_ids)
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: deliver_pending_file_tasks(
            session_factory=factory, publisher=overlap_publish), range(2)))
    assert sum(results) == 4
    assert drain() == [file_id] * 4
    with factory() as db:
        assert db.query(FileTaskOutbox).filter(FileTaskOutbox.delivered_at.is_(None)).count() == 0


def test_broker_confirmation_before_sql_failure_causes_safe_redelivery(file_database, postgres, broker):
    factory, file_id, _ = file_database
    with factory() as db:
        service.retry_embedding(db, file_id)
    with postgres.begin() as db:
        db.execute(text("""
            CREATE FUNCTION reject_delivery() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.delivered_at IS NOT NULL THEN RAISE EXCEPTION 'synthetic delivery commit failure'; END IF;
                RETURN NEW;
            END $$
        """))
        db.execute(text("CREATE TRIGGER reject_delivery BEFORE UPDATE ON file_task_outbox FOR EACH ROW EXECUTE FUNCTION reject_delivery()"))
    with pytest.raises(Exception, match="synthetic delivery commit failure"):
        deliver_pending_file_tasks(session_factory=factory, publisher=real_publish)
    with factory() as db:
        assert db.query(FileTaskOutbox).one().delivered_at is None
    assert drain() == [file_id]
    with postgres.begin() as db:
        db.execute(text("DROP TRIGGER reject_delivery ON file_task_outbox"))
    assert deliver_pending_file_tasks(session_factory=factory, publisher=real_publish) == 1
    assert drain() == [file_id]


def test_outbox_migration_backfills_and_rolls_back_atomically(postgres):
    from sqlalchemy import event, inspect
    from entry.migrate import upgrade

    upgrade(postgres, "0006_user_role_type")
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'migration-user','synthetic','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'migration-space',1)"))
        for index, status in enumerate(("pending", "processing", "success", "fail"), start=1):
            db.execute(text("INSERT INTO files(id,name,file_path,workspace_id,status) VALUES (:id,:name,:name,1,:status)"),
                       {"id": index, "name": status + ".txt", "status": status})
    def fail_backfill(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO file_task_outbox"):
            raise RuntimeError("synthetic backfill failure")
    event.listen(postgres, "before_cursor_execute", fail_backfill)
    try:
        with pytest.raises(RuntimeError, match="synthetic backfill failure"):
            upgrade(postgres)
    finally:
        event.remove(postgres, "before_cursor_execute", fail_backfill)
    assert not inspect(postgres).has_table("file_task_outbox")
    with postgres.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0006_user_role_type"
    upgrade(postgres)
    upgrade(postgres)
    with postgres.begin() as db:
        rows = db.execute(text("SELECT file_id,created_at,next_attempt_at FROM file_task_outbox ORDER BY file_id")).all()
        assert [row.file_id for row in rows] == [1, 2]
        assert all(row.created_at.tzinfo is not None and row.next_attempt_at.tzinfo is not None for row in rows)
        db.execute(text("DELETE FROM files WHERE id=1"))
        assert db.scalar(text("SELECT count(*) FROM file_task_outbox")) == 1
