import os
from pathlib import Path
import subprocess
import sys
import time

from app.infra.upload_staging import StagedUpload, cleanup_staging


def test_live_old_staging_is_never_cleaned(tmp_path):
    stage = StagedUpload(tmp_path, 'example.txt')
    Path(stage.path).write_bytes(b'active data')
    old = time.time() - 1000
    os.utime(stage.directory, (old, old))
    assert cleanup_staging(tmp_path, 1) == 0
    assert Path(stage.path).read_bytes() == b'active data'
    stage.close()
    assert list(tmp_path.iterdir()) == []


def test_crashed_process_staging_is_reclaimed(tmp_path):
    script = '''
import os
from pathlib import Path
from app.infra.upload_staging import StagedUpload
stage = StagedUpload(os.environ['TEST_STAGING_ROOT'])
Path(stage.path).write_bytes(b'unfinished')
os._exit(17)
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', script], cwd=root,
        env={**os.environ, 'TEST_STAGING_ROOT': str(tmp_path)}, capture_output=True)
    assert result.returncode == 17
    stage = next((tmp_path / '.staging').iterdir())
    old = time.time() - 1000
    os.utime(stage, (old, old))
    assert cleanup_staging(tmp_path, 1) == 1
    assert list(tmp_path.iterdir()) == []


def test_cleanup_does_not_touch_legacy_files_new_stages_or_symlinks(tmp_path):
    legacy = tmp_path / 'legacy-file'
    legacy.write_bytes(b'keep')
    stage = StagedUpload(tmp_path)
    stage.handle.close()  # Emulate a crash, while it is still too recent.
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'important').write_bytes(b'keep')
    link = tmp_path / '.staging' / ('a' * 32)
    link.symlink_to(outside, target_is_directory=True)
    assert cleanup_staging(tmp_path, 3600) == 0
    assert legacy.read_bytes() == b'keep'
    assert (outside / 'important').read_bytes() == b'keep'
    assert stage.directory.exists()


def test_constructor_fault_after_actual_lock_releases_lease(tmp_path,monkeypatch):
    import pytest
    from app.infra import upload_staging
    lock=upload_staging._lock
    handles=[]
    original=OSError('private-lease-initialization-fault')
    def fault(handle):
        lock(handle);handles.append(handle)
        raise original
    monkeypatch.setattr(upload_staging,'_lock',fault)
    with pytest.raises(OSError) as caught:
        StagedUpload(tmp_path,stage_id='a'*32)
    assert caught.value is original and handles[0].closed
    monkeypatch.setattr(upload_staging,'_lock',lock)
    retry=StagedUpload(tmp_path,stage_id='a'*32)
    retry.close()


def test_directory_cleanup_fault_still_closes_real_handle(tmp_path,monkeypatch):
    import pytest
    from app.infra import upload_staging
    stage=StagedUpload(tmp_path,stage_id='b'*32)
    if os.name=='nt':pytest.skip('POSIX remove-before-close lifecycle')
    def fail(*args,**kwargs):raise OSError('private-remove-fault')
    with monkeypatch.context() as patch:
        patch.setattr(upload_staging.shutil,'rmtree',fail)
        with pytest.raises(OSError):stage.close()
    assert stage.handle.closed
    retry=StagedUpload(tmp_path,stage_id='b'*32)
    retry.close()
