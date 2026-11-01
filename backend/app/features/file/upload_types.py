"""Typed inputs and byte-storage contracts for file publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from app.features.file.repository import FileRecordSpec
    from app.infra.upload_staging import StagedUpload


class UploadSource(Protocol):
    """Upload adapter following the ``save(path)`` contract."""

    filename: str

    @property
    def mimetype(self) -> str | None:
        """Return the upload's declared content type, when present."""
        ...

    def save(self, destination: str) -> None:
        """Write the payload to ``destination``."""
        ...


class UploadStorage(Protocol):
    """Object-store operations needed to publish or compensate an upload."""

    def upload_file(self, local_path: str, object_name: str) -> None:
        """Upload one staged object under its unique object name."""
        ...

    def delete_file(self, object_name: str) -> None:
        """Delete one object during upload compensation or cleanup."""
        ...

    def file_exists(self, object_name: str) -> bool:
        """Check whether one object is available for instant reuse."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class UploadScope:
    """Workspace, actor and target parent for one publication transaction."""

    workspace_id: int
    uploader_id: int
    parent_id: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class StagedFile:
    """Bounded bytes and derived metadata held under a staging lease."""

    staging: StagedUpload
    name: str
    size: int
    mime_type: str | None
    content_hash: str


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredUpload:
    """Durable reservation and file specification for a successfully PUT object."""

    reservation_id: str
    spec: FileRecordSpec


class PreflightData(TypedDict, total=False):
    """Preflight inputs decoded from an upload request."""

    filename: str
    total_size: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str | None
