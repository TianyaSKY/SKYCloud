"""Reclaim historical multipart directories without following directory links.

SQL ownership and live reservation discovery belong to the caller. POSIX scans
anchor each directory with a no-follow descriptor, including recursive removal.
"""
from contextlib import contextmanager
import errno
import os
import re
import shutil
import stat
import time

from loguru import logger


_DESCRIPTORS = os.open in os.supports_dir_fd and shutil.rmtree.avoids_symlink_attacks


def _ordinary_directory(info):
    return stat.S_ISDIR(info.st_mode) and not (
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


@contextmanager
def _directory(name, parent=None):
    handle = None
    try:
        try:
            if _DESCRIPTORS:
                handle = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            else:
                path = os.path.join(parent, name) if parent is not None else name
                if _ordinary_directory(os.stat(path, follow_symlinks=False)):
                    handle = path
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                raise
        yield handle
    finally:
        if isinstance(handle, int):
            os.close(handle)


def _child_stat(parent, name):
    if isinstance(parent, int):
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    return os.stat(os.path.join(parent, name), follow_symlinks=False)


def _remove_directory(parent, name) -> bool:
    try:
        if not _ordinary_directory(_child_stat(parent, name)):
            return False
    except FileNotFoundError:
        return False
    if isinstance(parent, int):
        shutil.rmtree(name, dir_fd=parent)
    else:
        shutil.rmtree(os.path.join(parent, name))
    return True


def remove_multipart_upload(root: str, workspace_id: int, upload_id: str) -> bool:
    """Remove only a validated owned upload; propagate failures for retry."""
    if workspace_id <= 0 or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', upload_id):
        raise ValueError('Invalid multipart cleanup identity')
    with _directory(root) as root_handle:
        if root_handle is None:
            return False
        with _directory(str(workspace_id), root_handle) as workspace_handle:
            return _remove_directory(workspace_handle, upload_id) if workspace_handle is not None else False


def remove_multipart_upload_safely(root: str, workspace_id: int, upload_id: str) -> None:
    """Best-effort filesystem cleanup after publication or cancellation."""
    try:
        remove_multipart_upload(root, workspace_id, upload_id)
    except OSError as exc:
        logger.warning('Multipart upload cleanup deferred: error_type={}', type(exc).__name__)


def cleanup_legacy_multipart(root: str, max_age_seconds: float,
                             active_ids: set[tuple[str, str]]) -> int:
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        with _directory(root) as root_handle:
            if root_handle is None:
                return 0
            for workspace in os.listdir(root_handle):
                try:
                    with _directory(workspace, root_handle) as workspace_handle:
                        if workspace_handle is None:
                            continue
                        for upload in os.listdir(workspace_handle):
                            if (workspace, upload) in active_ids:
                                continue
                            try:
                                info = _child_stat(workspace_handle, upload)
                                if not _ordinary_directory(info) or info.st_mtime >= cutoff:
                                    continue
                                removed += int(_remove_directory(workspace_handle, upload))
                            except OSError as exc:
                                logger.warning('Expired upload cleanup deferred: error_type={}', type(exc).__name__)
                        if not os.listdir(workspace_handle):
                            if isinstance(root_handle, int):
                                os.rmdir(workspace, dir_fd=root_handle)
                            else:
                                os.rmdir(os.path.join(root_handle, workspace))
                except OSError as exc:
                    logger.warning('Multipart workspace cleanup deferred: error_type={}', type(exc).__name__)
    except OSError as exc:
        logger.warning('Multipart root cleanup deferred: error_type={}', type(exc).__name__)
    return removed
