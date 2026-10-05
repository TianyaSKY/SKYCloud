"""Immutable runtime selection and private configuration update regressions."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from runtime_controller.engine import Engine, ControllerError, MissingRuntime
from runtime_controller.policy import Settings


def builder():
    path = Path(__file__).resolve().parents[2] / 'scripts/build-runtime-image.py'
    spec = importlib.util.spec_from_file_location('runtime_image_builder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_env_preserves_credentials_and_replaces_only_image(tmp_path):
    config = tmp_path / '.env'
    config.write_text('SECRET_KEY=existing-secret\nOPENCODE_IMAGE=old\nCHAT_API_KEY=existing-provider\n')
    config.chmod(0o644)
    builder().save_reference(config, 'sha256:' + 'a' * 64)
    assert config.read_text() == ('SECRET_KEY=existing-secret\nCHAT_API_KEY=existing-provider\n'
                                  'OPENCODE_IMAGE=sha256:' + 'a' * 64 + '\n')
    assert config.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.runtime-env-*'))


def test_private_env_refuses_symlink_without_changing_target(tmp_path):
    target = tmp_path / 'credentials'; target.write_text('SECRET_KEY=keep\n')
    config = tmp_path / '.env'; config.symlink_to(target)
    with pytest.raises(RuntimeError, match='symlink'):
        builder().save_reference(config, 'sha256:' + 'b' * 64)
    assert target.read_text() == 'SECRET_KEY=keep\n'


@pytest.mark.parametrize('reference', ['', 'runtime:latest', 'sha256:123'])
def test_controller_refuses_missing_or_mutable_runtime_image(monkeypatch, reference):
    monkeypatch.setenv('OPENCODE_IMAGE', reference)
    with pytest.raises(ValueError, match='pinned'):
        Settings.from_env()


def test_controller_missing_local_digest_never_attempts_registry_pull(monkeypatch):
    monkeypatch.setenv('OPENCODE_IMAGE', 'sha256:' + 'a' * 64)
    settings = Settings.from_env()
    client = Mock(); client.images.get.side_effect = MissingRuntime('not found')
    with pytest.raises(ControllerError, match='rebuild'):
        Engine(settings, client).ensure_image()
    client.images.pull.assert_not_called()


def test_restart_recreates_runtime_using_previous_policy(session, test_workspace, test_user):
    from unittest.mock import patch
    from types import SimpleNamespace
    from app.features.workspace import docker_service
    runtime = SimpleNamespace(container_id='a' * 64)
    container = Mock()
    container.attrs = {'Config': {'Image': docker_service.OPENCODE_IMAGE,
                                 'Labels': {'skycloud.runtime_policy': '3'}}}
    client = Mock(); client.containers.get.return_value = container
    with patch.object(docker_service.runtime_service, 'get_runtime', return_value=runtime), \
         patch.object(docker_service, '_client', return_value=client), \
         patch.object(docker_service, 'start', return_value=test_workspace) as start:
        assert docker_service.restart(session, test_workspace, test_user.id) is test_workspace
        start.assert_called_once_with(session, test_workspace, test_user.id)
    container.restart.assert_not_called()
