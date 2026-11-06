"""不可变运行时选择与私有配置更新回归测试."""
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.workspace import Workspace
from runtime_controller.engine import Engine, ControllerError, MissingRuntime
from runtime_controller.policy import Settings


def builder() -> Any:
    path = Path(__file__).resolve().parents[2] / 'scripts/build-runtime-image.py'
    spec = importlib.util.spec_from_file_location('runtime_image_builder', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_env_preserves_credentials_and_replaces_only_image(tmp_path: Path) -> None:
    config = tmp_path / '.env'
    config.write_text('SECRET_KEY=existing-secret\nOPENCODE_IMAGE=old\nCHAT_API_KEY=existing-provider\n')
    config.chmod(0o644)
    builder().save_reference(config, 'sha256:' + 'a' * 64)
    assert config.read_text() == ('SECRET_KEY=existing-secret\nCHAT_API_KEY=existing-provider\n'
                                  'OPENCODE_IMAGE=sha256:' + 'a' * 64 + '\n')
    assert config.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.runtime-env-*'))


def test_private_env_refuses_symlink_without_changing_target(tmp_path: Path) -> None:
    target = tmp_path / 'credentials'
    target.write_text('SECRET_KEY=keep\n')
    config = tmp_path / '.env'
    config.symlink_to(target)
    with pytest.raises(RuntimeError, match='symlink'):
        builder().save_reference(config, 'sha256:' + 'b' * 64)
    assert target.read_text() == 'SECRET_KEY=keep\n'


@pytest.mark.parametrize('reference', ['', 'runtime:latest', 'sha256:123'])
def test_controller_refuses_missing_or_mutable_runtime_image(monkeypatch: pytest.MonkeyPatch, reference: str) -> None:
    monkeypatch.setenv('OPENCODE_IMAGE', reference)
    with pytest.raises(ValueError, match='pinned'):
        Settings.from_env()


def test_controller_missing_local_digest_never_attempts_registry_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('OPENCODE_IMAGE', 'sha256:' + 'a' * 64)
    settings = Settings.from_env()
    client = Mock()
    client.images.get.side_effect = MissingRuntime('not found')
    with pytest.raises(ControllerError, match='rebuild'):
        Engine(settings, client).ensure_image()
    client.images.pull.assert_not_called()


def test_restart_recreates_runtime_using_previous_policy(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    from unittest.mock import patch
    from app.features.workspace import docker_service
    from app.models.opencode_runtime import OpenCodeRuntime
    runtime = OpenCodeRuntime(workspace_id=test_workspace.id, user_id=test_user.id, container_id='a' * 64, status='running')
    session.add(runtime)
    session.commit()
    container = {
        "id": "a" * 64,
        "name": "runtime",
        "status": "running",
        "image": docker_service.OPENCODE_IMAGE,
        "labels": {"skycloud.runtime_policy": "3"},
        "ports": {},
    }
    client = Mock()
    client.inspect.return_value = container
    with patch.object(docker_service.runtime_service, 'get_runtime', return_value=runtime), \
         patch.object(docker_service, '_client', return_value=client), \
         patch.object(docker_service, 'start', return_value=test_workspace) as start:
        assert docker_service.restart(session, test_workspace, test_user.id) is test_workspace
        start.assert_called_once_with(session, test_workspace, test_user.id)
    client.action.assert_not_called()


def test_start_reuses_the_current_container_after_a_completed_generation(
    session: Session, test_workspace: Workspace, test_user: User,
) -> None:
    """已完成的代系在下次启动时不得强行创建新代系."""
    from unittest.mock import patch
    from app.features.workspace import docker_service
    from app.models.opencode_runtime import OpenCodeRuntime
    from app.models.runtime_operation import RuntimeOperation

    runtime = OpenCodeRuntime(
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        container_id='a' * 64,
        status='running',
    )
    session.add(runtime)
    session.flush()
    session.add(RuntimeOperation(
        generation='g1',
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        runtime_id=runtime.id,
        candidate_container_id='a' * 64,
        state='complete',
    ))
    session.commit()

    container = {
        "id": "a" * 64,
        "name": "runtime",
        "status": "running",
        "image": docker_service.OPENCODE_IMAGE,
        "labels": {
            docker_service.RUNTIME_POLICY_LABEL: docker_service.POLICY_VERSION,
        },
        "ports": {},
    }
    client = Mock()
    client.inspect.return_value = container
    client.prepare.return_value = {**container, "id": "b" * 64}
    with patch.object(docker_service, '_client', return_value=client), \
         patch.object(docker_service, '_apply_runtime_config'):
        docker_service.start(session, test_workspace, test_user.id)

    client.prepare.assert_not_called()
    client.action.assert_called_once_with('a' * 64, 'start')
    session.expire_all()
    stored_runtime = session.get(OpenCodeRuntime, runtime.id)
    assert stored_runtime is not None
    assert stored_runtime.container_id == 'a' * 64
    assert session.query(RuntimeOperation).count() == 1
