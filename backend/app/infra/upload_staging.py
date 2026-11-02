"""私有临时上传暂存目录, 由活跃进程租约防止并发清理.

进程崩溃会自动释放 OS 文件锁。Worker 仅清理过期的未锁定
UUID 暂存目录, 绝不扫描上传根目录下的历史文件。
"""

import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO

from loguru import logger

from app.exceptions import ConflictError

# sys.platform (而非 os.name) 允许类型检查器跳过 Windows 专属桩差距。
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_STAGE_ID_PATTERN = r"[a-f0-9]{32}"
_LEASE_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)


def _lock(handle: IO[bytes]) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _staging_root(upload_root: str) -> Path:
    root = Path(upload_root) / ".staging"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    message = "Upload staging root must not be a symlink"
    if root.is_symlink():
        raise RuntimeError(message)
    return root


def _staging_identity(stage_id: str | None) -> str:
    identity = stage_id or uuid.uuid4().hex
    message = "Invalid upload staging identity"
    if not re.fullmatch(_STAGE_ID_PATTERN, identity):
        raise RuntimeError(message)
    return identity


def _staging_directory(root: Path, identity: str) -> Path:
    directory = root / identity
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    message = "Upload staging directory must not be a symlink"
    if directory.is_symlink():
        raise RuntimeError(message)
    return directory


def _close_descriptor(fd: int) -> None:
    try:
        os.close(fd)
    except OSError as exc:
        logger.warning(
            "Upload lease descriptor close failed: error_type={}",
            type(exc).__name__,
        )


def _close_handle(handle: IO[bytes]) -> None:
    # 尽最大努力关闭: 文件描述符关闭失败不得替换原始错误。
    try:
        handle.close()
    except Exception as exc:
        logger.warning(
            "Upload lease close failed: error_type={}", type(exc).__name__,
        )


def _prepare_lease(fd: int, handle: IO[bytes]) -> None:
    if os.fstat(fd).st_size == 0:
        handle.write(b"\0")
        handle.flush()
    try:
        _lock(handle)
    except (BlockingIOError, PermissionError) as exc:
        message = "Upload staging is busy"
        raise ConflictError(message) from exc


def _payload_extension(filename: str) -> str:
    # MIME 类型推断仍可使用原始扩展名。
    extension = Path(filename).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", extension):
        return ""
    return extension


class StagedUpload:
    """私有暂存目录, 其持有的租约可阻止并发清理任务."""

    directory: Path
    path: str
    handle: IO[bytes]

    def __init__(
        self,
        upload_root: str,
        filename: str = "upload",
        *,
        stage_id: str | None = None,
    ) -> None:
        """创建暂存目录并持有其进程租约."""
        root = _staging_root(upload_root)
        self.directory = _staging_directory(root, _staging_identity(stage_id))
        fd = os.open(self.directory / ".lease", _LEASE_FLAGS, 0o600)
        try:
            self.handle = os.fdopen(fd, "r+b")
        except BaseException:
            _close_descriptor(fd)
            raise
        try:
            _prepare_lease(fd, self.handle)
            self.path = str(
                self.directory / ("payload" + _payload_extension(filename)),
            )
        except BaseException:
            _close_handle(self.handle)
            raise

    def close(self) -> None:
        """释放租约并删除暂存目录."""
        if self.handle.closed:
            return
        try:
            if os.name != "nt":
                shutil.rmtree(self.directory, ignore_errors=True)
        finally:
            self.handle.close()
        if sys.platform == "win32":
            shutil.rmtree(self.directory, ignore_errors=True)
        with suppress(OSError):
            self.directory.parent.rmdir()


def cleanup_staging(upload_root: str, max_age_seconds: int) -> int:
    """删除过期的未加锁暂存目录; 返回已删除的目录数量."""
    root = Path(upload_root) / ".staging"
    if not root.is_dir() or root.is_symlink():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for directory in list(root.iterdir()):
        if (
            directory.is_symlink()
            or not re.fullmatch(_STAGE_ID_PATTERN, directory.name)
            or not directory.is_dir()
        ):
            continue
        handle = None
        try:
            if directory.stat().st_mtime > cutoff:
                continue
            lease = directory / ".lease"
            if lease.exists():
                fd = os.open(lease, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                handle = os.fdopen(fd, "r+b")
                _lock(handle)
            # POSIX 允许删除已加锁文件; Windows 则需要关闭的租约句柄。
            if sys.platform == "win32" and handle is not None:
                handle.close()
                handle = None
            shutil.rmtree(directory)
            removed += 1
        except (FileNotFoundError, BlockingIOError, PermissionError):
            continue
        finally:
            if handle is not None:
                handle.close()
    with suppress(OSError):
        root.rmdir()
    return removed


@contextmanager
def staging_guard(
    upload_root: str, stage_id: str | None,
) -> Generator[bool, None, None]:
    """在对账远程对象期间持有已终止进程的租约."""
    handle: IO[bytes] | None = None
    acquired = True
    try:
        if stage_id and re.fullmatch(_STAGE_ID_PATTERN, stage_id):
            directory = Path(upload_root) / ".staging" / stage_id
            if directory.is_symlink():
                acquired = False
            elif directory.exists():
                try:
                    fd = os.open(
                        directory / ".lease",
                        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    )
                    handle = os.fdopen(fd, "r+b")
                    _lock(handle)
                except FileNotFoundError:
                    pass
                except (BlockingIOError, PermissionError):
                    acquired = False
        yield acquired
    finally:
        if handle is not None:
            handle.close()


def close_staging_safely(staging: StagedUpload) -> None:
    """执行清理且绝不替换已提交的结果或上传失败错误."""
    # 此处任何关闭失败均次于上传结果, 故对其进行隔离。
    try:
        staging.close()
    except Exception as exc:
        logger.warning(
            "Upload staging cleanup deferred: error_type={}", type(exc).__name__,
        )
