"""在 Docker 构建或私有配置变更前拒绝篡改的源码测试."""
from io import BytesIO
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest


def builder() -> Any:
    path = Path(__file__).resolve().parents[2] / 'scripts/build-minio-image.py'
    spec = importlib.util.spec_from_file_location('minio_image_builder', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tampered_source_never_reaches_docker_build(monkeypatch: pytest.MonkeyPatch) -> None:
    module = builder()

    def fake_check_output(*_args: Any, **_kwargs: Any) -> str:
        return 'arm64\n'

    def fake_urlopen(*_args: Any, **_kwargs: Any) -> BytesIO:
        return BytesIO(b'tampered source')

    monkeypatch.setattr(module.subprocess, 'check_output', fake_check_output)
    build = Mock()
    monkeypatch.setattr(module.subprocess, 'run', build)
    monkeypatch.setattr(module.urllib.request, 'urlopen', fake_urlopen)
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        module.build()
    build.assert_not_called()



def test_minio_configuration_update_preserves_secrets_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / '.env'
    path.write_text('SECRET_KEY=keep\nMINIO_IMAGE=old\nOPENCODE_IMAGE=existing\nMINIO_SECRET_KEY=keep-too\n')
    builder().save_reference(path, 'sha256:' + 'a' * 64)
    assert path.read_text() == ('SECRET_KEY=keep\nOPENCODE_IMAGE=existing\nMINIO_SECRET_KEY=keep-too\nMINIO_IMAGE=sha256:' + 'a' * 64 + '\n')
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.minio-env-*'))


def test_minio_configuration_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / 'credentials'
    target.write_text('MINIO_SECRET_KEY=keep\n')
    path = tmp_path / '.env'
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match='symlink'):
        builder().save_reference(path, 'sha256:' + 'a' * 64)
    assert target.read_text() == 'MINIO_SECRET_KEY=keep\n'

