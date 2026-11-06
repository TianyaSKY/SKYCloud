import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, IO

import pytest

from app.infra import upload_staging
from app.infra.upload_staging import StagedUpload, cleanup_staging
from tests.test_llm_safe_diagnostics import (  # noqa: F401
    PRIVATE,
    assert_safe,
    logs,
)

_unused_fixture: object = logs


def test_live_old_staging_is_never_cleaned(tmp_path: Path) -> None:
    stage = StagedUpload(str(tmp_path), "example.txt")
    assert stage.path is not None
    Path(stage.path).write_bytes(b"active data")
    old = time.time() - 1000
    os.utime(stage.directory, (old, old))
    assert cleanup_staging(str(tmp_path), 1) == 0
    assert Path(stage.path).read_bytes() == b"active data"
    stage.close()
    assert list(tmp_path.iterdir()) == []


def test_crashed_process_staging_is_reclaimed(tmp_path: Path) -> None:
    script = """
import os
from pathlib import Path
from app.infra.upload_staging import StagedUpload
stage = StagedUpload(os.environ['TEST_STAGING_ROOT'])
assert stage.path is not None
Path(stage.path).write_bytes(b'unfinished')
os._exit(17)
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, "TEST_STAGING_ROOT": str(tmp_path)},
        capture_output=True,
    )
    assert result.returncode == 17
    stage = next((tmp_path / ".staging").iterdir())
    old = time.time() - 1000
    os.utime(stage, (old, old))
    assert cleanup_staging(str(tmp_path), 1) == 1
    assert list(tmp_path.iterdir()) == []


def test_cleanup_does_not_touch_legacy_files_new_stages_or_symlinks(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy-file"
    legacy.write_bytes(b"keep")
    stage = StagedUpload(str(tmp_path))
    stage.handle.close()  # Emulate a crash, while it is still too recent.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "important").write_bytes(b"keep")
    link = tmp_path / ".staging" / ("a" * 32)
    link.symlink_to(outside, target_is_directory=True)
    assert cleanup_staging(str(tmp_path), 3600) == 0
    assert legacy.read_bytes() == b"keep"
    assert (outside / "important").read_bytes() == b"keep"
    assert stage.directory.exists()


def test_constructor_fault_after_actual_lock_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = upload_staging._lock  # pyright: ignore[reportPrivateUsage]
    handles: list[IO[Any]] = []
    original = OSError("private-lease-initialization-fault")

    def fault(handle: IO[Any]) -> None:
        lock(handle)
        handles.append(handle)
        raise original

    monkeypatch.setattr(upload_staging, "_lock", fault)
    with pytest.raises(OSError) as caught:
        StagedUpload(str(tmp_path), stage_id="a" * 32)
    assert caught.value is original and handles[0].closed
    monkeypatch.setattr(upload_staging, "_lock", lock)
    retry = StagedUpload(str(tmp_path), stage_id="a" * 32)
    retry.close()


def test_directory_cleanup_fault_still_closes_real_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = StagedUpload(str(tmp_path), stage_id="b" * 32)
    if os.name == "nt":
        pytest.skip("POSIX remove-before-close lifecycle")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("private-remove-fault")

    with monkeypatch.context() as patch:
        patch.setattr(upload_staging.shutil, "rmtree", fail)
        with pytest.raises(OSError):
            stage.close()
    assert stage.handle.closed
    retry = StagedUpload(str(tmp_path), stage_id="b" * 32)
    retry.close()


def test_constructor_preserves_initialization_failure_when_handle_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    original = OSError(PRIVATE)
    fdopen: Any = os.fdopen
    fstat: Any = os.fstat
    handles: list[Any] = []
    lease_fds: set[int] = set()

    def open_with_failing_close(fd: int, *args: Any, **kwargs: Any) -> Any:
        handle: Any = fdopen(fd, *args, **kwargs)
        handles.append(handle)
        lease_fds.add(fd)
        close: Any = handle.close

        def fail_close() -> None:
            close()
            raise RuntimeError(PRIVATE)

        patch.setattr(handle, "close", fail_close)
        return handle

    def fail_initialization(fd: int) -> os.stat_result:
        if fd in lease_fds:
            raise original
        return fstat(fd)  # pyright: ignore[reportUnknownVariableType]

    with monkeypatch.context() as patch:
        patch.setattr(upload_staging.os, "fdopen", open_with_failing_close)
        patch.setattr(upload_staging.os, "fstat", fail_initialization)
        with pytest.raises(OSError) as caught:
            StagedUpload(str(tmp_path), stage_id="c" * 32)
        assert caught.value is original
        assert len(handles) == 1 and handles[0].closed

    retry = StagedUpload(str(tmp_path), stage_id="c" * 32)
    retry.close()
    assert list(tmp_path.iterdir()) == []
    assert_safe(logs, "error_type=RuntimeError")
