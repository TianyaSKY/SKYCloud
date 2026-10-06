"""Real PostgreSQL migration checks, isolated from any existing deployment."""

import concurrent.futures
import os
from pathlib import Path
import time
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.infra.schema import EXPECTED_REVISION, check_schema
from entry.migrate import migration_config, upgrade
from tests.integration.test_runtime_security import docker

pytestmark = pytest.mark.skipif(os.getenv("RUN_MIGRATION_TESTS") != "1", reason="Opt-in isolated PostgreSQL migrations")


@pytest.fixture
def postgres():
    name = "skycloud-migration-test-" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex + uuid.uuid4().hex
    engine = None
    try:
        docker("run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=" + password,
               "-p", "127.0.0.1::5432", "pgvector/pgvector:pg15")
        port = docker("port", name, "5432/tcp").stdout.strip().rsplit(":", 1)[1]
        engine = create_engine(f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise AssertionError("Isolated PostgreSQL did not start")
        yield engine
    finally:
        if engine:
            engine.dispose()
        docker("rm", "-f", name, check=False)


def test_fresh_upgrade_matches_models_and_supports_vectors(postgres):
    from app.infra.extensions import Base
    import app.models

    with pytest.raises(RuntimeError, match="unversioned"):
        check_schema(postgres)
    upgrade(postgres)
    check_schema(postgres)
    database = inspect(postgres)
    for table in Base.metadata.sorted_tables:
        assert database.has_table(table.name)
        columns = {column["name"] for column in database.get_columns(table.name)}
        assert set(table.columns.keys()) <= columns, table.name
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users")) == 0
        assert conn.scalar(text("SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector")) == 1
    upgrade(postgres)
    check_schema(postgres)


def test_unversioned_upgrade_repairs_old_columns_index_and_preserves_data(postgres):
    snapshot = Path(__file__).resolve().parents[2] / "migrations/0001_schema.sql"
    with postgres.begin() as conn:
        conn.exec_driver_sql(snapshot.read_text())
        conn.execute(text("INSERT INTO users (username,password_hash,role) VALUES ('existing','existing-hash','common')"))
        for table, column in [("files", "content_hash"), ("mcp_tokens", "token_value"), ("workspaces", "container_id"), ("workspaces", "status"), ("workspaces", "error_message"), ("assistant_runs", "permission_response")]:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(text("DROP INDEX file_vector_idx"))
        conn.execute(text("CREATE INDEX file_vector_idx ON files USING hnsw (vector_info vector_l2_ops)"))
    upgrade(postgres)
    check_schema(postgres)
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT password_hash FROM users WHERE username='existing'")) == "existing-hash"
        definition = conn.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname='file_vector_idx'"))
        assert "vector_cosine_ops" in definition
    database = inspect(postgres)
    assert "content_hash" in {c["name"] for c in database.get_columns("files")}
    assert "token_value" not in {c["name"] for c in database.get_columns("mcp_tokens")}
    assert "permission_response" in {c["name"] for c in database.get_columns("assistant_runs")}


def test_migration_failure_rolls_back_ddl_and_revision(postgres):
    # Incompatible historical table makes a later index operation fail.
    with postgres.begin() as conn:
        conn.execute(text("CREATE TABLE files (id SERIAL PRIMARY KEY)"))
    with pytest.raises(Exception):
        upgrade(postgres)
    database = inspect(postgres)
    assert not database.has_table("alembic_version")
    assert not database.has_table("users")
    assert "content_hash" not in {c["name"] for c in database.get_columns("files")}


def test_concurrent_upgrade_is_serialized(postgres):
    # Alembic's Python context is process-global; exercise separate processes.
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "DATABASE_URL": postgres.url.render_as_string(hide_password=False)}

    def run(_):
        result = subprocess.run([sys.executable, "-m", "entry.migrate"], cwd=root, env=env, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stderr

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(run, range(2)))
    check_schema(postgres)


def test_standard_alembic_cli_upgrade(postgres):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"), "upgrade", "head"],
        cwd=root, env={**os.environ, "DATABASE_URL": postgres.url.render_as_string(hide_password=False)},
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stderr
    check_schema(postgres)


def test_application_startup_works_with_read_only_database_role(postgres, tmp_path):
    from unittest.mock import patch
    from app import initialize_application

    upgrade(postgres)
    password = uuid.uuid4().hex
    with postgres.begin() as conn:
        conn.execute(text(f"CREATE ROLE migration_reader LOGIN PASSWORD '{password}'"))
        conn.execute(text("ALTER ROLE migration_reader SET default_transaction_read_only=on"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO migration_reader"))
        conn.execute(text("GRANT SELECT ON alembic_version, users TO migration_reader"))
    reader = create_engine(postgres.url.set(username="migration_reader", password=password))
    try:
        with patch("app.engine", reader), patch("app.UPLOAD_FOLDER", str(tmp_path / "uploads")):
            initialize_application()
        assert (tmp_path / "uploads").is_dir()
    finally:
        reader.dispose()


def test_password_change_is_atomic_and_concurrent_requests_cannot_reuse_old_password(postgres):
    from unittest.mock import patch
    from sqlalchemy.orm import Session
    from app.exceptions import BusinessRuleError
    from app.models.user import User
    from app.features.auth.user_service import change_password
    from app.features.auth.service import generate_token, decode_token
    from app.mcp.token_service import ensure_user_mcp_token, get_active_mcp_token

    upgrade(postgres)
    with Session(postgres) as session:
        user = User(username="session-owner", role="common")
        user.set_password("old-long-password")
        session.add(user)
        session.commit()
        user_id = user.id
        original = user.password_hash
        old_session = generate_token(user_id, user.auth_version)
        _, external = ensure_user_mcp_token(session, user_id)
        with patch.object(session, "commit", side_effect=RuntimeError("Commit failed")):
            with pytest.raises(RuntimeError, match="Commit failed"):
                change_password(session, user_id, "common", user_id, "old-long-password", "new-long-password")
        session.rollback()
        session.expire_all()
        assert session.get(User, user_id).password_hash == original
        assert decode_token(session, old_session) == str(user_id)
        assert get_active_mcp_token(session, external)

    def change(index):
        with Session(postgres) as session:
            try:
                change_password(session, user_id, "common", user_id, "old-long-password", f"new-long-password-{index}")
                return True
            except BusinessRuleError:
                session.rollback()
                return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(change, range(2)))
    assert results.count(True) == 1
    with Session(postgres) as session:
        assert session.get(User, user_id).auth_version == 1
        assert "revoked" in decode_token(session, old_session).lower()
        assert get_active_mcp_token(session, external) is None


def test_hash_only_mcp_migration_removes_legacy_raw_tokens_and_serializes_issuance(postgres):
    from sqlalchemy.orm import Session
    from app.models.user import User
    from app.models.mcp_token import McpToken
    from app.mcp.token_service import refresh_user_mcp_token, get_active_mcp_token

    upgrade(postgres, "0003_auth_version")
    with postgres.begin() as conn:
        user_id = conn.scalar(text("INSERT INTO users (username,password_hash,role) VALUES ('mcp-owner','hash','common') RETURNING id"))
        conn.execute(text("INSERT INTO mcp_tokens (user_id,token_hash,token_preview,token_value,expires_at) VALUES (:user,:hash,'legacy','legacy-raw-secret',now()+interval '1 year')"), {"user": user_id, "hash": McpToken.hash_token("legacy-raw-secret")})
    upgrade(postgres)
    columns = {c["name"] for c in inspect(postgres).get_columns("mcp_tokens")}
    assert "token_value" not in columns
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM mcp_tokens WHERE revoked_at IS NULL")) == 0

    def issue(_):
        with Session(postgres) as session:
            _, raw = refresh_user_mcp_token(session, user_id)
            return raw

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        raw_tokens = list(workers.map(issue, range(2)))
    assert len(set(raw_tokens)) == 2
    with Session(postgres) as session:
        assert sum(get_active_mcp_token(session, raw) is not None for raw in raw_tokens) == 1
        records = session.query(McpToken).all()
        assert all(raw not in str(record.to_dict()) for record in records for raw in raw_tokens)
        assert sum(record.revoked_at is None for record in records) == 1


def test_legacy_admin_gate_and_operator_recovery_preserve_workspace_data(postgres):
    from sqlalchemy.orm import Session
    from app.features.auth.legacy_admin import LEGACY_ADMIN_HASH, check_legacy_admin
    from app.features.auth.service import authenticate_user
    from entry.reset_admin import reset_admin

    upgrade(postgres)
    with postgres.begin() as conn:
        user_id = conn.scalar(text("INSERT INTO users (username,password_hash,role) VALUES ('admin',:hash,'admin') RETURNING id"), {"hash": LEGACY_ADMIN_HASH})
        conn.execute(text("INSERT INTO workspaces (name,owner_id) VALUES ('existing-workspace',:user)"), {"user": user_id})
    with pytest.raises(RuntimeError, match="entry.reset_admin"):
        check_legacy_admin(postgres)
    with Session(postgres) as session:
        assert authenticate_user(session, "admin", "admin123")[0] is None
        reset_admin(session, "admin", "new-unique-operator-password")
        assert authenticate_user(session, "admin", "new-unique-operator-password")[0]
    check_legacy_admin(postgres)
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM workspaces WHERE owner_id=:user"), {"user": user_id}) == 1


def test_wrong_version_blocks_startup_and_baseline_downgrade_is_rejected(postgres):
    upgrade(postgres, "0002_vector_index")
    with postgres.begin() as conn:
        command.downgrade(migration_config(conn), "0001_baseline")
    with pytest.raises(RuntimeError, match="version differs"):
        check_schema(postgres)
    with pytest.raises(RuntimeError, match="delete user data"):
        with postgres.begin() as conn:
            command.downgrade(migration_config(conn), "base")
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0001_baseline"
    upgrade(postgres)
    check_schema(postgres)


@pytest.mark.parametrize('shared', ['user', 'workspace', 'daily'])
def test_upload_quota_concurrent_admission_cannot_overbook(postgres, monkeypatch, shared):
    from sqlalchemy.orm import Session
    from app.features.file import upload_quota as quota
    from app.exceptions import BusinessRuleError
    upgrade(postgres)
    with postgres.begin() as conn:
        conn.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'quota-one','x','common'),(2,'quota-two','x','common')"))
        conn.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'quota-one',1),(2,'quota-two',1)"))
    monkeypatch.setattr(quota, 'USER_CAPACITY', 10 if shared == 'user' else 100)
    monkeypatch.setattr(quota, 'WORKSPACE_CAPACITY', 10 if shared == 'workspace' else 100)
    monkeypatch.setattr(quota, 'DAILY_ATTEMPTS', 1 if shared == 'daily' else 200)
    errors = []
    def admission(index):
        uid, wid = (1, index + 1) if shared != 'workspace' else (index + 1, 1)
        with Session(postgres) as session:
            try:
                return quota.reserve(session, wid, uid, 6).id
            except BusinessRuleError as exc:
                errors.append(str(exc))
                session.rollback()
                return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(admission, range(2)))
    assert sum(result is not None for result in results) == 1
    if shared == 'daily':
        assert errors == ['Daily upload allowance exceeded']
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT sum(size) FROM upload_reservations WHERE status='active'")) == 6
        assert conn.scalar(text("SELECT sum(attempts) FROM upload_daily_usage")) == 1


def test_upload_consumption_rollback_and_concurrent_completion(postgres):
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import IntegrityError
    from app.features.file import upload_quota as quota
    from app.models.file import File
    from app.models.upload_reservation import UploadReservation
    from app.exceptions import ConflictError
    upgrade(postgres)
    with postgres.begin() as conn:
        conn.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'quota-owner','x','common')"))
        conn.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'quota-space',1)"))
    with Session(postgres) as session:
        rid = quota.reserve(session, 1, 1, 6).id
        quota.consume(session, 1, 1, rid, 6)
        session.add(File(name=None, file_path='invalid', file_size=6, workspace_id=1, uploader_id=1))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.get(UploadReservation, rid).status == 'active'
    def completion(index):
        with Session(postgres) as session:
            try:
                quota.consume(session, 1, 1, rid, 6)
                session.add(File(name='completed', file_path='completed', file_size=6, workspace_id=1, uploader_id=1))
                session.commit()
                return True
            except ConflictError:
                session.rollback()
                return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(completion, range(2))) == [False, True]
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM files")) == 1
        assert conn.scalar(text("SELECT status FROM upload_reservations WHERE id=:id"), {'id': rid}) == 'completed'
        assert conn.scalar(text("SELECT sum(attempts) FROM upload_daily_usage")) == 1


def test_legacy_mcp_audit_redaction_preserves_event_metadata(postgres):
    upgrade(postgres, "0009_utc_timestamps")
    with postgres.begin() as db:
        db.execute(text("""INSERT INTO mcp_audit_logs
            (request_id,actor_type,tool_name,arguments_summary,entity_id,success,error_type,duration_ms)
            VALUES ('legacy','user','upload_file',:body,42,false,'tool_error',12),
                   ('empty','user','read_file_content',NULL,43,true,NULL,4)"""),
            {"body": '{"args":["PRIVATE_LEGACY_BODY_AND_TOKEN"]}'})
        events = db.execute(text("""SELECT id,request_id,actor_type,tool_name,entity_id,
            success,error_type,duration_ms,created_at FROM mcp_audit_logs ORDER BY id""")).all()
    upgrade(postgres)
    check_schema(postgres)
    with postgres.connect() as db:
        assert db.execute(text("""SELECT id,request_id,actor_type,tool_name,entity_id,
            success,error_type,duration_ms,created_at FROM mcp_audit_logs ORDER BY id""")).all() == events
        assert db.execute(text("SELECT arguments_summary FROM mcp_audit_logs ORDER BY id")).scalars().all() == [
            '{"schema":2,"legacy_redacted":true}', None]
    # Redaction executes only once; later safe summaries are not rewritten on startup.
    with postgres.begin() as db:
        db.execute(text("UPDATE mcp_audit_logs SET arguments_summary='safe-summary' WHERE request_id='legacy'"))
    upgrade(postgres)
    with postgres.connect() as db:
        assert db.scalar(text("SELECT arguments_summary FROM mcp_audit_logs WHERE request_id='legacy'")) == 'safe-summary'
    with pytest.raises(RuntimeError, match="pending object cleanup"):
        with postgres.begin() as db:
            command.downgrade(migration_config(db), "0009_utc_timestamps")
    check_schema(postgres)
    from alembic.script import ScriptDirectory
    from alembic.config import Config
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with pytest.raises(RuntimeError, match="cannot be reconstructed"):
        ScriptDirectory.from_config(config).get_revision("0010_redact_mcp_audit").module.downgrade()


def test_mcp_audit_redaction_failure_rolls_back_and_can_retry(postgres):
    upgrade(postgres, "0009_utc_timestamps")
    with postgres.begin() as db:
        db.execute(text("""INSERT INTO mcp_audit_logs(request_id,actor_type,tool_name,arguments_summary,success)
            VALUES ('first','user','upload_file','private-one',true),
                   ('second','user','upload_file','private-two',true)"""))
        db.execute(text("""CREATE FUNCTION reject_audit_redaction() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.request_id='second' THEN RAISE EXCEPTION 'injected audit migration failure'; END IF;
            RETURN NEW; END $$"""))
        db.execute(text("CREATE TRIGGER reject_audit BEFORE UPDATE ON mcp_audit_logs FOR EACH ROW EXECUTE FUNCTION reject_audit_redaction()"))
    with pytest.raises(Exception, match="injected audit migration failure"):
        upgrade(postgres)
    with postgres.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0009_utc_timestamps"
        assert db.execute(text("SELECT arguments_summary FROM mcp_audit_logs ORDER BY id")).scalars().all() == ['private-one','private-two']
    with postgres.begin() as db:
        db.execute(text("DROP TRIGGER reject_audit ON mcp_audit_logs"))
    upgrade(postgres)
    check_schema(postgres)
    with postgres.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM mcp_audit_logs WHERE arguments_summary LIKE '%private%'")) == 0
