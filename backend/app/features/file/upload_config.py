"""普通上传与分片上传的统一配置装配点."""

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
    """单文件上传的文件系统暂存根目录与默认字节上限."""

    upload_root: str
    max_upload_size: int


@dataclass(frozen=True, slots=True)
class MultipartSettings:
    """分片上传的文件系统根目录与分片参数上限."""

    upload_root: str
    multipart_root: str
    default_chunk_size: int
    max_chunk_size: int
    max_upload_size: int
    max_total_chunks: int


def upload_settings() -> UploadSettings:
    """基于当前上传根目录构建普通上传配置."""
    return UploadSettings(
        upload_root=UPLOAD_FOLDER,
        max_upload_size=MAX_UPLOAD_SIZE,
    )


def multipart_settings() -> MultipartSettings:
    """构建分片上传配置, 从当前上传根目录派生其分片根路径."""
    return MultipartSettings(
        upload_root=UPLOAD_FOLDER,
        multipart_root=str(Path(UPLOAD_FOLDER) / ".multipart"),
        default_chunk_size=DEFAULT_CHUNK_SIZE,
        max_chunk_size=MAX_CHUNK_SIZE,
        max_upload_size=MAX_UPLOAD_SIZE,
        max_total_chunks=MAX_TOTAL_CHUNKS,
    )
