from typing import Any
# pyright: reportPrivateUsage=false
"""与现有部署隔离的真实 PostgreSQL 初始化测试."""

import concurrent.futures
import os
from pathlib import Path
import time
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.infra.schema import EXPECTED_REVISION, check_schema
from entry.init_db import initialize_database
from tests.integration.test_runtime_security import POSTGRES_IMAGE, docker

pytestmark = pytest.mark.skipif(os.getenv("RUN_DATABASE_INIT_TESTS") != "1", reason="Opt-in isolated PostgreSQL initialization")


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
    name = "skycloud-init-test-" + uuid.uuid4().hex[:12]
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


def test_fresh_initialization_supports_postgres_features(postgres: Any) -> None:
    with pytest.raises(RuntimeError, match="unversioned"):
        check_schema(postgres)
    initialize_database(postgres)
    check_schema(postgres)
    with postgres.begin() as conn:
        user_id = conn.scalar(text(
            "INSERT INTO users (username, password_hash, role) "
            "VALUES ('init-owner', 'x', 'common') RETURNING id"
        ))
        workspace_id = conn.scalar(
            text("INSERT INTO workspaces (name, owner_id) VALUES ('init-workspace', :owner) RETURNING id"),
            {"owner": user_id},
        )
        conversation_id = conn.scalar(
            text("INSERT INTO assistant_conversations (workspace_id, user_id) VALUES (:workspace, :owner) RETURNING id"),
            {"workspace": workspace_id, "owner": user_id},
        )
        run_id = conn.scalar(
            text("INSERT INTO assistant_runs (conversation_id, engine) VALUES (:conversation, 'fast') RETURNING id"),
            {"conversation": conversation_id},
        )
        conn.execute(
            text("INSERT INTO assistant_permissions (run_id, permission_id, state, details_json) "
                 "VALUES (:run, 'permission-one', 'pending', '{}')"),
            {"run": run_id},
        )
    # 同一助手运行只允许一个未结束的权限请求; 完成后的请求不再占用唯一索引.
    with pytest.raises(IntegrityError):
        with postgres.begin() as conn:
            conn.execute(
                text("INSERT INTO assistant_permissions (run_id, permission_id, state, details_json) "
                     "VALUES (:run, 'permission-two', 'pending', '{}')"),
                {"run": run_id},
            )
    with postgres.begin() as conn:
        conn.execute(
            text("UPDATE assistant_permissions SET state='cancelled' "
                 "WHERE run_id=:run AND permission_id='permission-one'"),
            {"run": run_id},
        )
        conn.execute(
            text("INSERT INTO assistant_permissions "
                 "(run_id, permission_id, state, details_json, response) "
                 "VALUES (:run, 'permission-three', 'answered', '{}', 'once')"),
            {"run": run_id},
        )
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users")) == 1
        assert conn.scalar(text("SELECT count(*) FROM assistant_permissions")) == 2
        assert conn.scalar(text("SELECT value FROM sys_dict WHERE key='site_name'")) == "SKYCloud"
        assert conn.scalar(text("SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector")) == 1
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == EXPECTED_REVISION

    # 数据库触发器必须递增缓存版本，并在删除文件时暂存对象清理。
    with postgres.begin() as conn:
        assert conn.scalar(
            text("SELECT revision FROM sql_cache_revisions WHERE scope=:scope"),
            {"scope": f"user:profile:{user_id}"},
        ) == 1
        conn.execute(
            text("INSERT INTO files (name, file_path, file_size, workspace_id, uploader_id) "
                 "VALUES ('init.txt', 'objects/init.txt', 1, :workspace, :owner)"),
            {"workspace": workspace_id, "owner": user_id},
        )
        assert conn.scalar(
            text("SELECT revision FROM sql_cache_revisions WHERE scope=:scope"),
            {"scope": f"workspace:tree:{workspace_id}"},
        ) == 1
        conn.execute(text("UPDATE users SET username='init-renamed' WHERE id=:id"), {"id": user_id})
        assert conn.scalar(
            text("SELECT revision FROM sql_cache_revisions WHERE scope=:scope"),
            {"scope": f"user:profile:{user_id}"},
        ) == 2
        conn.execute(text("DELETE FROM files WHERE workspace_id=:workspace"), {"workspace": workspace_id})
        assert conn.scalar(text("SELECT object_name FROM file_object_deletions")) == "objects/init.txt"
        assert conn.scalar(
            text("SELECT revision FROM sql_cache_revisions WHERE scope=:scope"),
            {"scope": f"workspace:tree:{workspace_id}"},
        ) == 2


def test_initialized_schema_allows_only_one_active_mcp_token(postgres: Any) -> None:
    initialize_database(postgres)
    with postgres.begin() as conn:
        user_id = conn.scalar(text(
            "INSERT INTO users (username, password_hash) "
            "VALUES ('token-owner', 'x') RETURNING id"
        ))
        conn.execute(
            text("INSERT INTO mcp_tokens (user_id, token_hash, token_preview, expires_at) "
                 "VALUES (:user_id, 'first-hash', 'first', now() + interval '1 day')"),
            {"user_id": user_id},
        )

    with pytest.raises(IntegrityError):
        with postgres.begin() as conn:
            conn.execute(
                text("INSERT INTO mcp_tokens (user_id, token_hash, token_preview, expires_at) "
                     "VALUES (:user_id, 'second-hash', 'second', now() + interval '1 day')"),
                {"user_id": user_id},
            )

    with postgres.begin() as conn:
        conn.execute(text("UPDATE mcp_tokens SET revoked_at = now() WHERE user_id = :id"), {"id": user_id})
        conn.execute(
            text("INSERT INTO mcp_tokens (user_id, token_hash, token_preview, expires_at) "
                 "VALUES (:user_id, 'second-hash', 'second', now() + interval '1 day')"),
            {"user_id": user_id},
        )
        assert conn.scalar(text("SELECT count(*) FROM mcp_tokens WHERE revoked_at IS NULL")) == 1


def test_initialization_is_repeatable_and_preserves_existing_data(postgres: Any) -> None:
    from sqlalchemy.orm import Session
    from app.models.user import User

    initialize_database(postgres)
    with Session(postgres) as session:
        user = User(username="existing-owner", role="admin")
        user.set_password("existing-owner-password")
        session.add(user)
        session.commit()
        user_id = user.id
    with postgres.begin() as conn:
        conn.execute(text("INSERT INTO sys_dict (key, value) VALUES ('custom_marker', 'keep-me')"))
        conn.execute(
            text("INSERT INTO workspaces (name, owner_id) VALUES ('existing-workspace', :owner)"),
            {"owner": user_id},
        )
        conn.execute(text("CREATE TABLE deployment_marker (note TEXT)"))
        conn.execute(text("INSERT INTO deployment_marker VALUES ('untouched')"))

    initialize_database(postgres)

    check_schema(postgres)
    with Session(postgres) as session:
        existing = session.get(User, user_id)
        assert existing is not None
        assert existing.username == "existing-owner"
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users")) == 1
        assert conn.scalar(text("SELECT value FROM sys_dict WHERE key='custom_marker'")) == "keep-me"
        assert conn.scalar(text("SELECT count(*) FROM workspaces")) == 1
        assert conn.scalar(text("SELECT note FROM deployment_marker")) == "untouched"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == EXPECTED_REVISION

def test_unversioned_database_initialization_is_rejected_without_changes(postgres: Any) -> None:
    with postgres.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO users VALUES (7, 'preserved')"))

    with pytest.raises(RuntimeError, match="Unversioned"):
        initialize_database(postgres)

    database = inspect(postgres)
    assert database.get_table_names() == ["users"]
    with postgres.connect() as conn:
        assert conn.execute(text("SELECT id, username FROM users")).one() == (7, "preserved")


def test_wrong_revision_initialization_is_rejected_without_changes(postgres: Any) -> None:
    with postgres.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0019_sql_cache_revisions')"))

    with pytest.raises(RuntimeError, match="version"):
        initialize_database(postgres)

    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0019_sql_cache_revisions"
        assert conn.scalar(text("SELECT to_regclass('public.users')")) is None


def test_failed_initialization_rolls_back_database_objects(postgres: Any) -> None:
    with postgres.begin() as conn:
        conn.exec_driver_sql(
            "CREATE FUNCTION public.bump_sql_cache_revisions() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
        )

    with pytest.raises(ProgrammingError, match="already exists"):
        initialize_database(postgres)

    with postgres.connect() as conn:
        assert inspect(conn).get_table_names(schema="public") == []
        assert conn.scalar(text("SELECT to_regprocedure('public.bump_sql_cache_revisions()')")) is not None
        assert not conn.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')"))


def test_concurrent_cli_initialization_is_serialized(postgres: Any) -> None:
    # 初始化在独立进程中通过公共 CLI 执行, 保证咨询锁跨进程串行化.
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "DATABASE_URL": postgres.url.render_as_string(hide_password=False)}

    def run(_: Any):
        result = subprocess.run([sys.executable, "-m", "entry.init_db"], cwd=root, env=env, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stderr

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(run, range(2)))
    check_schema(postgres)
    with postgres.connect() as conn:
        assert conn.scalar(text("SELECT value FROM sys_dict WHERE key='site_name'")) == "SKYCloud"


def test_application_startup_works_with_read_only_database_role(postgres: Any, tmp_path: Any) -> None:
    from unittest.mock import patch
    from app import initialize_application

    initialize_database(postgres)
    password = uuid.uuid4().hex
    with postgres.begin() as conn:
        conn.execute(text(f"CREATE ROLE init_reader LOGIN PASSWORD '{password}'"))
        conn.execute(text("ALTER ROLE init_reader SET default_transaction_read_only=on"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO init_reader"))
        conn.execute(text("GRANT SELECT ON alembic_version, users TO init_reader"))
    reader = create_engine(postgres.url.set(username="init_reader", password=password))
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

    initialize_database(postgres)
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

    initialize_database(postgres)
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


@pytest.mark.parametrize('shared', ['user', 'workspace', 'daily'])
def test_upload_quota_concurrent_admission_cannot_overbook(postgres: Any, monkeypatch: Any, shared: Any) -> None:
    from sqlalchemy.orm import Session
    from app.features.file import upload_quota as quota
    from app.exceptions import BusinessRuleError
    initialize_database(postgres)
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
    initialize_database(postgres)
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


