"""Single configuration assembly point for ordinary and multipart uploads."""

from dataclasses import dataclass
from pathlib import Path

from app.infra.extensions import UPLOAD_FOLDER as _UPLOAD_FOLDER
from app.infra.upload_limits import FILE_LIMIT, positive_limit

UPLOAD_FOLDER = _UPLOAD_FOLDER

DEFAULT_CHUNK_SIZE = positive_limit("UPLOAD_CHUNK_SIZE", 2 * 1024 * 1024)
MAX_CHUNK_SIZE = positive_limit("UPLOAD_MAX_CHUNK_SIZE", 64 * 1024 * 1024)
MAX_UPLOAD_SIZE = positive_limit("UPLOAD_MAX_FILE_SIZE", FILE_LIMIT)
MAX_TOTAL_CHUNKS = positive_limit("UPLOAD_MAX_TOTAL_CHUNKS", 10000)


@dataclass(frozen=True, slots=True)
class UploadSettings:
    """Filesystem staging root and default byte ceiling for one upload."""

    upload_root: str
    max_upload_size: int


@dataclass(frozen=True, slots=True)
class MultipartSettings:
    """Filesystem roots and chunk ceilings for multipart uploads."""

    upload_root: str
    multipart_root: str
    default_chunk_size: int
    max_chunk_size: int
    max_upload_size: int
    max_total_chunks: int


def upload_settings() -> UploadSettings:
    """Build ordinary-upload settings from the current upload root."""
    return UploadSettings(
        upload_root=UPLOAD_FOLDER,
        max_upload_size=MAX_UPLOAD_SIZE,
    )


def multipart_settings() -> MultipartSettings:
    """Build multipart settings, deriving its root from the current upload root."""
    return MultipartSettings(
        upload_root=UPLOAD_FOLDER,
        multipart_root=str(Path(UPLOAD_FOLDER) / ".multipart"),
        default_chunk_size=DEFAULT_CHUNK_SIZE,
        max_chunk_size=MAX_CHUNK_SIZE,
        max_upload_size=MAX_UPLOAD_SIZE,
        max_total_chunks=MAX_TOTAL_CHUNKS,
    )
