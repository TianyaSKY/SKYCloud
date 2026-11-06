"""集成测试套件必须跨平台解析并创建其 fixture 目录测试."""
import os
from pathlib import Path
import tempfile

import pytest

from tests.integration import fixture_paths


def test_fixture_dir_honours_the_override_and_creates_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / 'nested' / 'fixtures'
    monkeypatch.setenv('SKYCLOUD_TEST_FIXTURE_DIR', str(target))

    resolved = fixture_paths.fixture_dir()

    assert resolved == target
    assert resolved.is_dir()


def test_fixture_dir_defaults_to_an_existing_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('SKYCLOUD_TEST_FIXTURE_DIR', raising=False)

    resolved = fixture_paths.fixture_dir()

    assert resolved.is_dir()
    if not Path('/private/tmp').is_dir():
        assert resolved == Path(tempfile.gettempdir())
    assert fixture_paths.FIXTURE_DIR.is_dir()
    assert os.path.isabs(str(resolved))
