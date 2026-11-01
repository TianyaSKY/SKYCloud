"""Local multipart bytes and atomic publication; no database or authorization.

Callers validate upload identity/ownership and hold the reservation transaction.
Only a complete, size-checked temporary file replaces an existing destination.
"""

import errno
import json
import os
import stat
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from loguru import logger

from app.exceptions import BusinessRuleError
from app.infra.upload_adapter import UploadInput, copy_upload_bounded
from app.infra.upload_cleanup import remove_partial_safely
from app.infra.upload_limits import copy_bounded

_INVALID_DIRECTORY = "Invalid multipart directory"
_INVALID_CHUNK = "Invalid stored chunk"


def upload_directory(root: str, workspace_id: int, upload_id: str) -> str:
    """Return the multipart directory for one workspace upload."""
    return str(Path(root) / str(workspace_id) / upload_id)


def chunks_directory(upload_dir: str) -> str:
    """Return the chunk directory inside a multipart upload directory."""
    return str(Path(upload_dir) / "chunks")


def metadata_path(upload_dir: str) -> str:
    """Return the metadata file path inside a multipart upload directory."""
    return str(Path(upload_dir) / "meta.json")


_DESCRIPTORS = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")


def _ordinary(info: os.stat_result, mode: Callable[[int], bool]) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return mode(info.st_mode) and not (attributes & reparse)


def _lexical_absolute(value: str) -> Path:
    """Normalize an absolute path lexically without following directory links."""
    return Path(os.path.normpath(Path(value).absolute()))


@contextmanager
def _chunks_handle(
    chunks_dir: str, root: str | None, *, create: bool = False,
) -> Generator[int | str, None, None]:
    """Anchor byte operations beneath the multipart root without directory links."""
    root_path = _lexical_absolute(root or chunks_dir)
    chunks_path = _lexical_absolute(chunks_dir)
    if not chunks_path.is_relative_to(root_path):
        raise BusinessRuleError(_INVALID_DIRECTORY)
    parts = chunks_path.relative_to(root_path).parts
    if create:
        root_path.mkdir(parents=True, exist_ok=True)
    handles: list[int] = []
    try:
        if _DESCRIPTORS:
            yield _open_anchored(root_path, parts, handles, create=create)
        else:
            yield str(_validated_path(root_path, parts, create=create))
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise BusinessRuleError(_INVALID_DIRECTORY) from exc
        raise
    finally:
        for descriptor in reversed(handles):
            os.close(descriptor)


def _open_anchored(
    root: Path, parts: tuple[str, ...], handles: list[int], *, create: bool,
) -> int:
    handle = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    handles.append(handle)
    for part in parts:
        if create:
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=handle)
        handle = os.open(
            part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle,
        )
        handles.append(handle)
    return handle


def _validated_path(root: Path, parts: tuple[str, ...], *, create: bool) -> Path:
    path = root
    for part in (".", *parts):
        path = path / part
        if create:
            with suppress(FileExistsError):
                path.mkdir(mode=0o700)
        if not _ordinary(path.stat(follow_symlinks=False), stat.S_ISDIR):
            raise BusinessRuleError(_INVALID_DIRECTORY)
    return path


def _chunk_stat(handle: int | str, name: str) -> os.stat_result:
    if isinstance(handle, int):
        return os.stat(name, dir_fd=handle, follow_symlinks=False)
    return (Path(handle) / name).stat(follow_symlinks=False)


def _require_regular(info: os.stat_result) -> None:
    if not _ordinary(info, stat.S_ISREG):
        raise BusinessRuleError(_INVALID_CHUNK)


@contextmanager
def _chunk_source(
    handle: int | str, name: str,
) -> Generator[BinaryIO, None, None]:
    descriptor: int | None = None
    try:
        if isinstance(handle, int):
            # NONBLOCK avoids hanging on a substituted FIFO before fstat rejects it.
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=handle,
            )
        else:
            _require_regular(_chunk_stat(handle, name))
            descriptor = os.open(str(Path(handle) / name), os.O_RDONLY)
        _require_regular(os.fstat(descriptor))
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise BusinessRuleError(_INVALID_CHUNK) from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _chunk_names(handle: int | str) -> list[str]:
    if isinstance(handle, int):
        with os.scandir(handle) as entries:
            return [entry.name for entry in entries]
    return [entry.name for entry in Path(handle).iterdir()]


def list_chunks(chunks_dir: str, *, root: str | None = None) -> list[int]:
    """List the stored chunk indexes without following file or directory links."""
    try:
        with _chunks_handle(chunks_dir, root) as handle:
            return sorted(
                int(name[:-5])
                for name in _chunk_names(handle)
                if name.endswith(".part")
                and name[:-5].isdigit()
                and _ordinary(_chunk_stat(handle, name), stat.S_ISREG)
            )
    except FileNotFoundError:
        return []


@contextmanager
def _atomic_destination(destination: str) -> Generator[str, None, None]:
    temporary = f"{destination}.{uuid.uuid4().hex}.tmp"
    try:
        yield temporary
        Path(temporary).replace(destination)
    finally:
        remove_partial_safely(temporary)


def _remove_temporary(handle: int | str, name: str) -> None:
    try:
        if isinstance(handle, int):
            os.unlink(name, dir_fd=handle)
        else:
            (Path(handle) / name).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(
            "Partial upload cleanup deferred (error_type={})", type(exc).__name__,
        )


@contextmanager
def _atomic_owned_target(
    handle: int | str, name: str,
) -> Generator[BinaryIO, None, None]:
    """Create, publish and reclaim a temporary file relative to one directory."""
    temporary = name + "." + uuid.uuid4().hex + ".tmp"
    descriptor: int | None = None
    try:
        if isinstance(handle, int):
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=handle,
            )
        else:
            descriptor = os.open(
                str(Path(handle) / temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        with os.fdopen(descriptor, "wb", closefd=False) as target:
            yield target
        if isinstance(handle, int):
            os.replace(temporary, name, src_dir_fd=handle, dst_dir_fd=handle)
        else:
            (Path(handle) / temporary).replace(Path(handle) / name)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _remove_temporary(handle, temporary)


def prepare_chunks(chunks_dir: str, *, root: str) -> None:
    """Create the chunk directory chain beneath the multipart root."""
    with _chunks_handle(chunks_dir, root, create=True):
        pass


def read_metadata(path: str, *, root: str | None = None) -> dict[str, Any]:
    """Read chunk metadata relative to the multipart root."""
    target = Path(path)
    with _chunks_handle(str(target.parent), root) as handle, _chunk_source(
        handle, target.name,
    ) as source:
        return json.load(source)


def write_metadata(
    path: str, metadata: Mapping[str, Any], *, root: str | None = None,
) -> None:
    """Write chunk metadata atomically relative to the multipart root."""
    target = Path(path)
    with _chunks_handle(str(target.parent), root, create=True) as handle, (
        _atomic_owned_target(handle, target.name)
    ) as destination:
        destination.write(
            json.dumps(dict(metadata), ensure_ascii=False).encode("utf-8"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SaveChunkParams:
    """One chunk write: identity, upload, size limits and anchor root."""

    index: int
    upload: UploadInput
    expected_size: int
    max_size: int
    root: str | None = None


def save_chunk(chunks_dir: str, params: SaveChunkParams) -> None:
    """Write one bounded chunk atomically under the chunks directory."""
    with _chunks_handle(chunks_dir, params.root, create=True) as handle, (
        _atomic_owned_target(handle, f"{params.index}.part")
    ) as target:
        copy_upload_bounded(
            params.upload, target, min(params.expected_size, params.max_size),
        )
        actual_size = target.tell()
        if actual_size != params.expected_size:
            message = (
                f"Invalid chunk size for index {params.index}: "
                f"expected {params.expected_size}, got {actual_size}"
            )
            raise BusinessRuleError(message)


@dataclass(frozen=True, slots=True, kw_only=True)
class AssembleParams:
    """Expected chunk layout for one atomic assembly."""

    total_chunks: int
    chunk_size: int
    total_size: int
    root: str | None = None


def assemble(chunks_dir: str, destination: str, params: AssembleParams) -> None:
    """Concatenate every chunk into ``destination`` and publish atomically."""
    with _chunks_handle(chunks_dir, params.root) as handle, _atomic_destination(
        destination,
    ) as temporary:
        with Path(temporary).open("wb") as target:
            for index in range(params.total_chunks):
                expected = min(
                    params.chunk_size,
                    params.total_size - index * params.chunk_size,
                )
                with _chunk_source(handle, f"{index}.part") as source:
                    if copy_bounded(source, target, expected) != expected:
                        message = "Invalid stored chunk size"
                        raise BusinessRuleError(message)
        actual_size = Path(temporary).stat().st_size
        if actual_size != params.total_size:
            message = (
                "Assembled file size mismatch: "
                f"expected {params.total_size}, got {actual_size}"
            )
            raise BusinessRuleError(message)
