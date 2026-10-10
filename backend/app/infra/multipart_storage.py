"""Local multipart bytes and atomic publication; no database or authorization.

Callers validate upload identity/ownership and hold the reservation transaction.
Only a complete, size-checked temporary file replaces an existing destination.
"""
from contextlib import contextmanager
import json
import os
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


def list_chunks(chunks_dir: str) -> list[int]:
    if not os.path.isdir(chunks_dir):
        return []
    return sorted(int(name[:-5]) for name in os.listdir(chunks_dir)
                  if name.endswith(".part") and name[:-5].isdigit())


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
             chunk_size: int, total_size: int) -> None:
    with _atomic_destination(destination) as temporary:
        with open(temporary, "wb") as target:
            for index in range(total_chunks):
                expected = min(chunk_size, total_size - index * chunk_size)
                with open(os.path.join(chunks_dir, f"{index}.part"), "rb") as source:
                    if copy_bounded(source, target, expected) != expected:
                        raise BusinessRuleError("Invalid stored chunk size")
        actual_size = os.path.getsize(temporary)
        if actual_size != total_size:
            raise BusinessRuleError(
                f"Assembled file size mismatch: expected {total_size}, got {actual_size}"
            )
