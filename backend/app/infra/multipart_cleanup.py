"""回收历史分块上传目录, 不跟随目录软链接.

SQL 归属与活跃预留查找属于调用方职责。POSIX 扫描
通过防跟随的文件描述符锚定各个目录, 包括递归删除操作。
"""

import errno
import os
import re
import shutil
import stat
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

_DESCRIPTORS = os.open in os.supports_dir_fd and shutil.rmtree.avoids_symlink_attacks


def _ordinary_directory(info: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISDIR(info.st_mode) and not (attributes & reparse)


@contextmanager
def _directory(
    name: str, parent: int | str | None = None,
) -> Generator[int | str | None, None, None]:
    handle: int | str | None = None
    try:
        try:
            if _DESCRIPTORS:
                handle = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent if isinstance(parent, int) else None,
                )
            else:
                path = Path(name)
                if isinstance(parent, str):
                    path = Path(parent) / name
                if _ordinary_directory(path.stat(follow_symlinks=False)):
                    handle = str(path)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                raise
        yield handle
    finally:
        if isinstance(handle, int):
            os.close(handle)


def _child_stat(parent: int | str, name: str) -> os.stat_result:
    if isinstance(parent, int):
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    return (Path(parent) / name).stat(follow_symlinks=False)


def _remove_directory(parent: int | str, name: str) -> bool:
    try:
        if not _ordinary_directory(_child_stat(parent, name)):
            return False
    except FileNotFoundError:
        return False
    if isinstance(parent, int):
        shutil.rmtree(name, dir_fd=parent)
    else:
        shutil.rmtree(Path(parent) / name)
    return True


def remove_multipart_upload(
    root: str, workspace_id: int, upload_id: str,
) -> bool:
    """仅删除经过验证且归属合法的上传目录; 抛出失败供重试."""
    if workspace_id <= 0 or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", upload_id):
        message = "Invalid multipart cleanup identity"
        raise ValueError(message)
    with _directory(root) as root_handle:
        if root_handle is None:
            return False
        with _directory(str(workspace_id), root_handle) as workspace_handle:
            if workspace_handle is None:
                return False
            return _remove_directory(workspace_handle, upload_id)


def remove_multipart_upload_safely(
    root: str, workspace_id: int, upload_id: str,
) -> None:
    """发布或取消后尽最大努力执行文件系统清理."""
    try:
        remove_multipart_upload(root, workspace_id, upload_id)
    except OSError as exc:
        logger.warning(
            "Multipart upload cleanup deferred: error_type={}", type(exc).__name__,
        )


def _list_names(handle: int | str) -> list[str]:
    if isinstance(handle, int):
        with os.scandir(handle) as entries:
            return [entry.name for entry in entries]
    return [entry.name for entry in Path(handle).iterdir()]


def _remove_workspace_if_empty(
    root_handle: int | str, workspace_handle: int | str, workspace: str,
) -> None:
    if _list_names(workspace_handle):
        return
    if isinstance(root_handle, int):
        os.rmdir(workspace, dir_fd=root_handle)
    else:
        (Path(root_handle) / workspace).rmdir()


def _cleanup_uploads(
    workspace_handle: int | str,
    workspace: str,
    cutoff: float,
    active_ids: set[tuple[str, str]],
) -> int:
    removed = 0
    for upload in _list_names(workspace_handle):
        if (workspace, upload) in active_ids:
            continue
        try:
            info = _child_stat(workspace_handle, upload)
            if not _ordinary_directory(info) or info.st_mtime >= cutoff:
                continue
            removed += int(_remove_directory(workspace_handle, upload))
        except OSError as exc:
            logger.warning(
                "Expired upload cleanup deferred: error_type={}",
                type(exc).__name__,
            )
    return removed


def _cleanup_workspaces(
    root_handle: int | str | None,
    cutoff: float,
    active_ids: set[tuple[str, str]],
) -> int:
    if root_handle is None:
        return 0
    removed = 0
    for workspace in _list_names(root_handle):
        try:
            with _directory(workspace, root_handle) as workspace_handle:
                if workspace_handle is None:
                    continue
                removed += _cleanup_uploads(
                    workspace_handle, workspace, cutoff, active_ids,
                )
                _remove_workspace_if_empty(
                    root_handle, workspace_handle, workspace,
                )
        except OSError as exc:
            logger.warning(
                "Multipart workspace cleanup deferred: error_type={}",
                type(exc).__name__,
            )
    return removed


def cleanup_legacy_multipart(
    root: str, max_age_seconds: float, active_ids: set[tuple[str, str]],
) -> int:
    """清理位于 ``root`` 之下已过期的遗留工作区/上传目录."""
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        with _directory(root) as root_handle:
            removed = _cleanup_workspaces(root_handle, cutoff, active_ids)
    except OSError as exc:
        logger.warning(
            "Multipart root cleanup deferred: error_type={}", type(exc).__name__,
        )
    return removed
