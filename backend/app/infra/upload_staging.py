"""Private temporary uploads, protected against cleanup by live process leases.

A process crash releases the OS file lock. The Worker only removes old unlocked
UUID staging directories, and never scans historical files in the upload root.
"""
from contextlib import contextmanager

from app.exceptions import ConflictError
from loguru import logger

import os
from pathlib import Path
import re
import shutil
import time
import uuid

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


def _lock(handle):
    if os.name == 'nt':
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class StagedUpload:
    def __init__(self, upload_root, filename='upload', *, stage_id=None):
        root = Path(upload_root) / '.staging'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise RuntimeError('Upload staging root must not be a symlink')
        stage_id = stage_id or uuid.uuid4().hex
        if not re.fullmatch(r'[a-f0-9]{32}', stage_id):
            raise RuntimeError('Invalid upload staging identity')
        self.directory = root / stage_id
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise RuntimeError('Upload staging directory must not be a symlink')
        fd = os.open(self.directory / '.lease', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            self.handle = os.fdopen(fd, 'r+b')
        except BaseException:
            try:
                os.close(fd)
            except OSError as exc:
                logger.warning('Upload lease descriptor close failed: error_type={}', type(exc).__name__)
            raise
        try:
            if os.fstat(fd).st_size == 0:
                self.handle.write(b'\0')
                self.handle.flush()
            try:
                _lock(self.handle)
            except (BlockingIOError, PermissionError) as exc:
                raise ConflictError('Upload staging is busy') from exc
            # MIME guessing can still use the original extension.
            extension = Path(filename).suffix
            if not re.fullmatch(r'\.[A-Za-z0-9]{1,16}', extension):
                extension = ''
            self.path = str(self.directory / ('payload' + extension))
        except BaseException:
            try:
                self.handle.close()
            except Exception as exc:
                logger.warning('Upload lease close failed: error_type={}', type(exc).__name__)
            raise

    def close(self):
        if self.handle.closed:
            return
        try:
            if os.name != 'nt':
                shutil.rmtree(self.directory, ignore_errors=True)
        finally:
            self.handle.close()
        if os.name == 'nt':
            shutil.rmtree(self.directory, ignore_errors=True)
        try:
            self.directory.parent.rmdir()
        except OSError:
            pass


def cleanup_staging(upload_root, max_age_seconds):
    root = Path(upload_root) / '.staging'
    if not root.is_dir() or root.is_symlink():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for directory in list(root.iterdir()):
        if directory.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', directory.name) or not directory.is_dir():
            continue
        handle = None
        try:
            if directory.stat().st_mtime > cutoff:
                continue
            lease = directory / '.lease'
            if lease.exists():
                fd = os.open(lease, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
                handle = os.fdopen(fd, 'r+b')
                _lock(handle)
            # POSIX permits deleting locked files; Windows needs a closed lease.
            if os.name == 'nt' and handle is not None:
                handle.close()
                handle = None
            shutil.rmtree(directory)
            removed += 1
        except (FileNotFoundError, BlockingIOError, PermissionError):
            continue
        finally:
            if handle is not None:
                handle.close()
    try:
        root.rmdir()
    except OSError:
        pass
    return removed


@contextmanager
def staging_guard(upload_root, stage_id):
    """Hold a dead process's lease while reconciling its remote object."""
    handle = None
    acquired = True
    try:
        if stage_id and re.fullmatch(r'[a-f0-9]{32}', stage_id):
            directory = Path(upload_root) / '.staging' / stage_id
            if directory.is_symlink():
                acquired = False
            elif directory.exists():
                try:
                    fd = os.open(directory / '.lease', os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
                    handle = os.fdopen(fd, 'r+b')
                    _lock(handle)
                except FileNotFoundError:
                    pass
                except (BlockingIOError, PermissionError):
                    acquired = False
        yield acquired
    finally:
        if handle is not None:
            handle.close()


def close_staging_safely(staging):
    """Cleanup must not replace a committed result or the upload's business error."""
    try:
        staging.close()
    except Exception as exc:
        logger.warning('Upload staging cleanup deferred: error_type={}', type(exc).__name__)
