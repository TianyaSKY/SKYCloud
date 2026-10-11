"""Local multipart bytes and atomic publication; no database or authorization.

Callers validate upload identity/ownership and hold the reservation transaction.
Only a complete, size-checked temporary file replaces an existing destination.
"""
from contextlib import contextmanager
import json
import errno
import os
import stat
import uuid
from typing import Any

from app.exceptions import BusinessRuleError
from app.infra.upload_adapter import save_bounded
from app.infra.upload_limits import copy_bounded
from app.infra.upload_cleanup import remove_partial_safely


def upload_directory(root: str, workspace_id: int, upload_id: str) -> str:
    return os.path.join(root, str(workspace_id), upload_id)


def chunks_directory(upload_dir: str) -> str:
    return os.path.join(upload_dir, "chunks")


def metadata_path(upload_dir: str) -> str:
    return os.path.join(upload_dir, "meta.json")


_DESCRIPTORS = os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW')


def _ordinary(info, mode) -> bool:
    return mode(info.st_mode) and not (
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


@contextmanager
def _chunks_handle(chunks_dir: str, root: str | None):
    """Anchor reads beneath the configured multipart root, without directory links."""
    root = os.path.abspath(root or chunks_dir)
    chunks_dir = os.path.abspath(chunks_dir)
    if os.path.commonpath([root, chunks_dir]) != root:
        raise BusinessRuleError('Invalid multipart directory')
    parts = os.path.relpath(chunks_dir, root).split(os.sep)
    handles = []
    try:
        if _DESCRIPTORS:
            handle = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(handle)
            for part in parts:
                if part != '.':
                    handle = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
                    handles.append(handle)
        else:
            handle = root
            for part in ['.'] + parts:
                handle = os.path.join(handle, part)
                if not _ordinary(os.stat(handle, follow_symlinks=False), stat.S_ISDIR):
                    raise BusinessRuleError('Invalid multipart directory')
        yield handle
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise BusinessRuleError('Invalid multipart directory') from exc
        raise
    finally:
        for handle in reversed(handles):
            os.close(handle)


def _chunk_stat(handle, name):
    if isinstance(handle, int):
        return os.stat(name, dir_fd=handle, follow_symlinks=False)
    return os.stat(os.path.join(handle, name), follow_symlinks=False)


@contextmanager
def _chunk_source(handle, name):
    descriptor = None
    try:
        if isinstance(handle, int):
            # NONBLOCK avoids hanging on a substituted FIFO before fstat rejects it.
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handle)
        else:
            if not _ordinary(_chunk_stat(handle, name), stat.S_ISREG):
                raise BusinessRuleError('Invalid stored chunk')
            descriptor = os.open(os.path.join(handle, name), os.O_RDONLY)
        if not _ordinary(os.fstat(descriptor), stat.S_ISREG):
            raise BusinessRuleError('Invalid stored chunk')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            yield source
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise BusinessRuleError('Invalid stored chunk') from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def list_chunks(chunks_dir: str, *, root: str | None = None) -> list[int]:
    try:
        with _chunks_handle(chunks_dir, root) as handle:
            return sorted(int(name[:-5]) for name in os.listdir(handle)
                          if name.endswith('.part') and name[:-5].isdigit()
                          and _ordinary(_chunk_stat(handle, name), stat.S_ISREG))
    except FileNotFoundError:
        return []


@contextmanager
def _atomic_destination(destination: str):
    temporary = f"{destination}.{uuid.uuid4().hex}.tmp"
    try:
        yield temporary
        os.replace(temporary, destination)
    finally:
        remove_partial_safely(temporary)


def read_metadata(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source)


def write_metadata(path: str, metadata: dict[str, Any]) -> None:
    with _atomic_destination(path) as temporary:
        with open(temporary, "w", encoding="utf-8") as target:
            json.dump(metadata, target, ensure_ascii=False)


def save_chunk(chunks_dir: str, index: int, upload: Any, expected_size: int, max_size: int) -> None:
    os.makedirs(chunks_dir, exist_ok=True)
    destination = os.path.join(chunks_dir, f"{index}.part")
    with _atomic_destination(destination) as temporary:
        save_bounded(upload, temporary, min(expected_size, max_size))
        actual_size = os.path.getsize(temporary)
        if actual_size != expected_size:
            raise BusinessRuleError(
                f"Invalid chunk size for index {index}: expected {expected_size}, got {actual_size}"
            )


def assemble(chunks_dir: str, destination: str, *, total_chunks: int,
             chunk_size: int, total_size: int, root: str | None = None) -> None:
    with _chunks_handle(chunks_dir, root) as handle, _atomic_destination(destination) as temporary:
        with open(temporary, "wb") as target:
            for index in range(total_chunks):
                expected = min(chunk_size, total_size - index * chunk_size)
                with _chunk_source(handle, f"{index}.part") as source:
                    if copy_bounded(source, target, expected) != expected:
                        raise BusinessRuleError("Invalid stored chunk size")
        actual_size = os.path.getsize(temporary)
        if actual_size != total_size:
            raise BusinessRuleError(
                f"Assembled file size mismatch: expected {total_size}, got {actual_size}"
            )
