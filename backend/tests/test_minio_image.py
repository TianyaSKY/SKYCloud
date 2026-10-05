"""Reject tampered source before Docker build or private configuration changes."""
from io import BytesIO
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


def builder():
    path = Path(__file__).resolve().parents[2] / 'scripts/build-minio-image.py'
    spec = importlib.util.spec_from_file_location('minio_image_builder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tampered_source_never_reaches_docker_build(monkeypatch):
    module = builder()
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: 'arm64\n')
    build = Mock()
    monkeypatch.setattr(module.subprocess, 'run', build)
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: BytesIO(b'tampered source'))
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        module.build()
    build.assert_not_called()


def test_minio_configuration_update_preserves_secrets_and_is_private(tmp_path):
    path = tmp_path / '.env'
    path.write_text('SECRET_KEY=keep\nMINIO_IMAGE=old\nOPENCODE_IMAGE=existing\nMINIO_SECRET_KEY=keep-too\n')
    builder().save_reference(path, 'sha256:' + 'a' * 64)
    assert path.read_text() == ('SECRET_KEY=keep\nOPENCODE_IMAGE=existing\nMINIO_SECRET_KEY=keep-too\nMINIO_IMAGE=sha256:' + 'a' * 64 + '\n')
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.minio-env-*'))


def test_minio_configuration_refuses_symlink(tmp_path):
    target = tmp_path / 'credentials'
    target.write_text('MINIO_SECRET_KEY=keep\n')
    path = tmp_path / '.env'
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match='symlink'):
        builder().save_reference(path, 'sha256:' + 'a' * 64)
    assert target.read_text() == 'MINIO_SECRET_KEY=keep\n'
