"""The integration harness must resolve and create its fixture directory portably."""
import os
import tempfile
from pathlib import Path

from tests.integration import fixture_paths


def test_fixture_dir_honours_the_override_and_creates_it(tmp_path, monkeypatch):
    target = tmp_path / 'nested' / 'fixtures'
    monkeypatch.setenv('SKYCLOUD_TEST_FIXTURE_DIR', str(target))

    resolved = fixture_paths.fixture_dir()

    assert resolved == target
    assert resolved.is_dir()


def test_fixture_dir_defaults_to_an_existing_directory(monkeypatch):
    monkeypatch.delenv('SKYCLOUD_TEST_FIXTURE_DIR', raising=False)

    resolved = fixture_paths.fixture_dir()

    assert resolved.is_dir()
    if not Path('/private/tmp').is_dir():
        assert resolved == Path(tempfile.gettempdir())
    assert fixture_paths.FIXTURE_DIR.is_dir()
    assert os.path.isabs(str(resolved))
