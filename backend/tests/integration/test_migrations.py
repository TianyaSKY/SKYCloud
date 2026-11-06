from typing import Any
# pyright: reportPrivateUsage=false
"""与现有部署隔离的真实 PostgreSQL 迁移测试."""

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
from tests.integration.test_runtime_security import POSTGRES_IMAGE, docker

pytestmark = pytest.mark.skipif(os.getenv("RUN_MIGRATION_TESTS") != "1", reason="Opt-in isolated PostgreSQL migrations")


@pytest.mark.parametrize('denied', [False, True])
def test_lifecycle_lock_disconnect_keeps_outcome_and_allows_next_owner(postgres: Any, denied: Any) -> None:
    from sqlalchemy.orm import Session
    from app.exceptions import PermissionDeniedError
    from app.features.workspace.runtime_execution import execution

    original = PermissionDeniedError('permission revoked')

    def operation():
        with Session(postgres) as session:
            with execution(session, 12, 34):
                with postgres.begin() as control:
                    owners = control.scalars(text("SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted")).all()
                    assert len(owners) == 1
                    assert control.scalar(text('SELECT pg_terminate_backend(:pid)'), {'pid': owners[0]})
                if denied:
                    raise original
                return 'accepted'

    if denied:
        with pytest.raises(PermissionDeniedError) as error:
            operation()
        assert error.value is original
    else:
        assert operation() == 'accepted'
    with Session(postgres) as successor:
        with execution(successor, 12, 34):
            assert successor.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted")) == 1
    with postgres.connect() as control:
        assert control.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted")) == 0


@pytest.fixture
def postgres():
    name = "skycloud-migration-test-" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex + uuid.uuid4().hex
    engine = None
    try:
        docker("run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=" + password,
               "-p", "127.0.0.1::5432", POSTGRES_IMAGE)
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
        docker("rm", "-f", "-v", name, check=False)


def test_fresh_upgrade_matches_models_and_supports_vectors(postgres: Any) -> None:
    from app.infra.extensions import Base
    import app.models
    _ = app.models

    with pytest.raises(RuntimeError, match="unversioned"):
        check_schema(postgres)
    upgrade(postgres)
    check_schema(postgres)
    database = inspect(postgres)
    for table in Base.metadata.sorted_tables:
        assert database.has_table(table.name)
        columns = {column["name"] for column in database.get_columns(table.name)}
        assert set(table.columns.keys()) <= columns, table.name

    run_columns = {
        column["name"] for column in database.get_columns("assistant_runs")
    }
    assert {"pending_permission_id", "permission_response"}.isdisjoint(
        run_columns,
    )
    permission_primary_key = database.get_pk_constraint("assistant_permissions")
    assert permission_primary_key["constrained_columns"] == [
        "run_id",
        "permission_id",
    ]
    permission_checks = {
        check["name"]
        for check in database.get_check_constraints("assistant_permissions")
    }
    assert permission_checks == {
        "ck_assistant_permission_state",
        "ck_assistant_permission_response",
    }
    permission_foreign_keys = database.get_foreign_keys(
        "assistant_permissions",
    )
    assert any(
        foreign_key["referred_table"] == "assistant_runs"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in permission_foreign_keys
    )
    permission_index = next(
        index
        for index in database.get_indexes("assistant_permissions")
        if index["name"] == "uq_assistant_permission_open_run"
    )
    assert permission_index["unique"] is True
    with postgres.connect() as conn:
        predicate = conn.scalar(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname='uq_assistant_permission_open_run'"
        ))
    assert "WHERE" in predicate and "pending" in predicate and "answered" in predicate
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users")) == 0
        assert conn.scalar(text("SELECT value FROM sys_dict WHERE key='site_name'")) == "SKYCloud"
        assert conn.scalar(text("SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector")) == 1
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == EXPECTED_REVISION
        cache_triggers = set(conn.scalars(text(
            "SELECT tgname FROM pg_trigger "
            "WHERE NOT tgisinternal AND tgname LIKE '%sql_cache_revision'"
        )))
        assert "sys_dict_sql_cache_revision" not in cache_triggers
        assert {
            "users_sql_cache_revision",
            "files_sql_cache_revision",
            "folder_sql_cache_revision",
        } <= cache_triggers
    upgrade(postgres)
    check_schema(postgres)
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users")) == 0
        assert conn.scalar(text("SELECT value FROM sys_dict WHERE key='site_name'")) == "SKYCloud"

def test_unversioned_database_upgrade_is_rejected_without_changes(postgres: Any) -> None:
    with postgres.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO users VALUES (7, 'preserved')"))

    with pytest.raises(RuntimeError, match="(?i)unversioned|empty database"):
        upgrade(postgres)

    database = inspect(postgres)
    assert database.get_table_names() == ["users"]
    with postgres.connect() as conn:
        assert conn.execute(text("SELECT id, username FROM users")).one() == (7, "preserved")

def test_old_revision_upgrade_is_rejected_without_changes(postgres: Any) -> None:
    with postgres.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0019_sql_cache_revisions')"))

    with pytest.raises(RuntimeError, match="revision|version"):
        upgrade(postgres)

    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0019_sql_cache_revisions"
        assert conn.scalar(text("SELECT to_regclass('public.users')")) is None






def test_concurrent_upgrade_is_serialized(postgres: Any) -> None:
    # Alembic 的 Python 上下文是进程全局的; 在独立进程中执行.
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "DATABASE_URL": postgres.url.render_as_string(hide_password=False)}

    def run(_: Any):
        result = subprocess.run([sys.executable, "-m", "entry.migrate"], cwd=root, env=env, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stderr

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(run, range(2)))
    check_schema(postgres)


def test_standard_alembic_cli_upgrade(postgres: Any) -> None:
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


def test_application_startup_works_with_read_only_database_role(postgres: Any, tmp_path: Any) -> None:
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


def test_password_change_is_atomic_and_concurrent_requests_cannot_reuse_old_password(postgres: Any) -> None:
    from unittest.mock import patch
    from sqlalchemy.orm import Session
    from app.exceptions import BusinessRuleError
    from app.models.user import User
    from app.features.auth.user_service import ChangePasswordParams, change_password
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
        assert old_session is not None and external is not None
        with patch.object(session, "commit", side_effect=RuntimeError("Commit failed")):
            with pytest.raises(RuntimeError, match="Commit failed"):
                change_password(session, ChangePasswordParams(actor_id=user_id, actor_role="common", user_id=user_id, old_password="old-long-password", new_password="new-long-password"))
        session.rollback()
        session.expire_all()
        curr_user = session.get(User, user_id)
        assert curr_user is not None
        assert curr_user.password_hash == original
        assert decode_token(session, old_session) == str(user_id)
        assert get_active_mcp_token(session, external)

    def change(index: Any):
        with Session(postgres) as session:
            try:
                change_password(session, ChangePasswordParams(actor_id=user_id, actor_role="common", user_id=user_id, old_password="old-long-password", new_password=f"new-long-password-{index}"))
                return True
            except BusinessRuleError:
                session.rollback()
                return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(change, range(2)))
    assert results.count(True) == 1
    with Session(postgres) as session:
        reloaded_user = session.get(User, user_id)
        assert reloaded_user is not None
        assert reloaded_user.auth_version == 1
        assert "revoked" in decode_token(session, old_session).lower()
        assert get_active_mcp_token(session, external) is None




def test_legacy_admin_gate_and_operator_recovery_preserve_workspace_data(postgres: Any) -> None:
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


def test_baseline_downgrade_is_rejected(postgres: Any) -> None:
    upgrade(postgres)
    with pytest.raises(RuntimeError, match="downgrade|backup"):
        with postgres.begin() as conn:
            command.downgrade(migration_config(conn), "base")
    check_schema(postgres)
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == EXPECTED_REVISION



@pytest.mark.parametrize('shared', ['user', 'workspace', 'daily'])
def test_upload_quota_concurrent_admission_cannot_overbook(postgres: Any, monkeypatch: Any, shared: Any) -> None:
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
    errors: list[str] = []
    def admission(index: Any):
        uid, wid = (1, index + 1) if shared != 'workspace' else (index + 1, 1)
        with Session(postgres) as session:
            try:
                return quota.reserve(session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=6)).id
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


def test_upload_consumption_rollback_and_concurrent_completion(postgres: Any) -> None:
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
        rid = quota.reserve(session, quota.ReserveParams(workspace_id=1, user_id=1, size=6)).id
        quota.consume(session, 1, 1, rid, 6)
        session.add(File(name=None, file_path='invalid', file_size=6, workspace_id=1, uploader_id=1))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        reservation = session.get(UploadReservation, rid)
        assert reservation is not None
        assert reservation.status == 'active'
    def completion(index: Any):
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


