import importlib.util
import os
from pathlib import Path

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.infra.extensions import Base
from app.infra.secrets import required_secret
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.folder import Folder
from entry.bootstrap_admin import bootstrap_admin


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize("value", ["", "short", "replace-with-a-long-random-secret", "sky_cloud_secret_key_dev_only"])
def test_signing_secret_fails_closed(monkeypatch, value):
    monkeypatch.setenv("SECRET_KEY", value)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        required_secret("SECRET_KEY")


def test_first_admin_bootstrap_is_atomic_and_cannot_repeat(session):
    user_id = bootstrap_admin(session, "operator", "unique-long-admin-password")
    user = session.get(User, user_id)
    assert user.role == "admin" and user.check_password("unique-long-admin-password")
    workspace = session.query(Workspace).filter_by(owner_id=user_id).one()
    assert session.query(WorkspaceMember).filter_by(workspace_id=workspace.id, user_id=user_id, role="admin").count() == 1
    assert session.query(Folder).filter_by(workspace_id=workspace.id, parent_id=None).count() == 1
    session.rollback()
    with pytest.raises(ValueError, match="already exists"):
        bootstrap_admin(session, "another", "another-long-admin-password")
    assert session.query(User).count() == 1


def test_bootstrap_rejects_weak_password_without_writes(session):
    with pytest.raises(ValueError):
        bootstrap_admin(session, "operator", "admin123")
    assert session.query(User).count() == 0


def test_env_generation_is_private_unique_and_never_overwrites(tmp_path):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("generate_env", root / "scripts/generate-env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "deployment.env"
    module.generate(output, root / ".env.example")
    values = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert len({values[name] for name in module.SECRET_NAMES}) == len(module.SECRET_NAMES)
    assert all(len(values[name]) >= 32 for name in module.SECRET_NAMES)
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        module.generate(output, root / ".env.example")
    assert output.read_bytes() == original


@pytest.mark.parametrize("value", [None, "", "guest", "minioadmin", "pwd", "skycloud", "replace-with-a-secret"])
def test_default_infrastructure_credentials_are_rejected(value):
    from app.infra.secrets import configured_password
    with pytest.raises(RuntimeError, match="explicitly configured"):
        configured_password("Infrastructure password", value)


def test_rabbitmq_adapter_rejects_missing_and_encoded_default_passwords(monkeypatch):
    from app.infra.task_queue import _connection_parameters
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    monkeypatch.delenv("RABBITMQ_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        _connection_parameters()
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:%67%75%65%73%74@host/")
    with pytest.raises(RuntimeError):
        _connection_parameters()


def test_storage_adapter_rejects_default_password_before_connecting():
    from unittest.mock import patch
    from app.infra.storage import MinIOStorageClient
    with patch("app.infra.storage.Minio") as client:
        with pytest.raises(RuntimeError):
            MinIOStorageClient(access_key="operator", secret_key="minioadmin")
    client.assert_not_called()


def test_startup_rejects_reused_master_secret(monkeypatch):
    from sqlalchemy.engine import make_url
    from app.infra.secrets import check_deployment_secrets
    monkeypatch.setenv("RUNTIME_CONTROLLER_TOKEN", os.environ["SECRET_KEY"])
    with pytest.raises(RuntimeError, match="independent"):
        check_deployment_secrets(make_url("postgresql://operator:long-database-password@db/database"))


def test_legacy_administrator_is_blocked_until_operator_reset(session):
    from app.features.auth.legacy_admin import LEGACY_ADMIN_HASH, check_legacy_admin
    from app.features.auth.service import authenticate_user, generate_token, decode_token
    from entry.reset_admin import reset_admin
    user = User(username="admin", role="admin", password_hash=LEGACY_ADMIN_HASH)
    session.add(user)
    session.commit()
    before = generate_token(user.id, user.auth_version)
    assert authenticate_user(session, "admin", "admin123")[0] is None
    with pytest.raises(RuntimeError, match="entry.reset_admin"):
        check_legacy_admin(session.get_bind())
    reset_admin(session, "admin", "new-unique-operator-password")
    check_legacy_admin(session.get_bind())
    assert authenticate_user(session, "admin", "new-unique-operator-password")[0]
    assert "revoked" in decode_token(session, before).lower()


def test_operator_reset_refuses_common_users_and_weak_passwords(session):
    from entry.reset_admin import reset_admin
    user = User(username="common", role="common")
    user.set_password("original-password")
    session.add(user)
    session.commit()
    with pytest.raises(ValueError, match="Administrator not found"):
        reset_admin(session, "common", "new-long-password")
    with pytest.raises(ValueError, match="at least 12"):
        reset_admin(session, "common", "admin123")
    assert user.check_password("original-password")
