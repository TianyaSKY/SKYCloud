"""Real database rollback and file-index concurrency behavior."""
import concurrent.futures
import os
import threading

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.infra.indexing import handler
from app.models.file import File
from app.models.file_chunk import FileChunk
from app.models.user import User
from app.models.workspace import Workspace
from entry.migrate import upgrade
from tests.integration.test_migrations import postgres  # noqa: F401


@pytest.fixture
def file_database(postgres, monkeypatch):
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    monkeypatch.setattr(handler, "SessionLocal", factory)
    with factory() as db:
        user = User(username="worker-user", password_hash="synthetic-unused", role="common")
        db.add(user); db.flush()
        workspace = Workspace(name="Worker", owner_id=user.id)
        db.add(workspace); db.flush()
        file = File(name="worker.txt", file_path="worker.txt", workspace_id=workspace.id,
                    uploader_id=user.id, status="processing")
        db.add(file); db.flush()
        file_id, workspace_id = file.id, workspace.id
        db.commit()
        yield factory, file_id, workspace_id


def test_failure_status_database_error_propagates_and_rolls_back(file_database, postgres):
    factory, file_id, _ = file_database
    with postgres.begin() as db:
        db.execute(text("""
            CREATE FUNCTION reject_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.status = 'fail' THEN RAISE EXCEPTION 'synthetic status failure'; END IF;
                RETURN NEW;
            END $$
        """))
        db.execute(text("CREATE TRIGGER reject_failure BEFORE UPDATE ON files FOR EACH ROW EXECUTE FUNCTION reject_failure()"))
    with pytest.raises(Exception, match="synthetic status failure"):
        handler.mark_file_failed(file_id, RuntimeError("synthetic provider failure"))
    with factory() as db:
        assert db.get(File, file_id).status == "processing"


def test_overlapping_index_results_replace_chunks_atomically(file_database, postgres):
    factory, file_id, workspace_id = file_database
    barrier = threading.Barrier(2)
    def overlap(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement:
            barrier.wait(timeout=5)
    event.listen(postgres, "before_cursor_execute", overlap)
    def result(label):
        return handler.FileIndexResult(
            file_id, workspace_id, "worker.txt", label, [1.0] + [0.0] * 1023,
            [handler.ChunkIndexResult(i, None, label + str(i), [1.0] + [0.0] * 1023) for i in range(2)],
        )
    try:
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(handler._save_index_result, result(label)) for label in ("A", "B")]
            for future in futures:
                future.result(timeout=10)
    finally:
        event.remove(postgres, "before_cursor_execute", overlap)
    with factory() as db:
        file = db.get(File, file_id)
        chunks = db.query(FileChunk).filter_by(file_id=file_id).order_by(FileChunk.chunk_index).all()
        assert file.status == "success"
        assert len(chunks) == 2
        assert [c.content for c in chunks] == [file.description + str(i) for i in range(2)]


