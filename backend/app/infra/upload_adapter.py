"""上传适配器: 向服务层暴露遗留的 ``save(path)`` 契约.

历史服务最初针对 Werkzeug 的 ``FileStorage`` 风格对象编写;
适配器使 FastAPI 的 ``UploadFile`` 和 Base64 载荷保持相同的调用签名。
"""

import base64
import binascii
import mimetypes
import re
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol, TypeAlias

from fastapi import UploadFile
from werkzeug.datastructures import FileStorage

from app.exceptions import BusinessRuleError, PayloadTooLargeError
from app.infra.upload_cleanup import remove_partial_safely
from app.infra.upload_limits import FILE_LIMIT, copy_bounded


class LegacyDiskUpload(Protocol):
    """将自身持久化保存至目标路径的遗留上传对象协议."""

    def save(self, destination: str) -> None:
        """将上传内容写入 ``destination``."""


class FastAPIUploadAdapter:
    """将 ``UploadFile`` 包装为具备 filename/mimetype/save 属性的对象."""

    def __init__(self, upload_file: UploadFile) -> None:
        """在遗留契约下暴露有界的 FastAPI 上传文件."""
        self.upload_file = upload_file
        self.filename = upload_file.filename or ""
        self.mimetype = upload_file.content_type

    def save(self, destination: str, max_bytes: int = FILE_LIMIT) -> None:
        """从请求中最多复制 ``max_bytes`` 字节到 ``destination``."""
        # 同一请求可能保存多次, 因此重置指针到开头。
        self.upload_file.file.seek(0)
        try:
            with Path(destination).open("wb") as target:
                copy_bounded(self.upload_file.file, target, max_bytes)
        except BaseException:
            remove_partial_safely(destination)
            raise


class Base64UploadAdapter:
    """解析 data URI 或纯 Base64 载荷并补全文件扩展名."""

    def __init__(self, base64_str: str, filename: str | None = None) -> None:
        """解码载荷头部并推导安全的文件名."""
        self._base64_str = base64_str
        self.mimetype = "application/octet-stream"
        self._data: str

        # 支持 data:image/png;base64,xxxx 格式。
        if "," in base64_str:
            header, data = base64_str.split(",", 1)
            self._data = data
            match = re.match(r"data:([^;]+);base64", header)
            if match:
                self.mimetype = match.group(1)
        else:
            self._data = base64_str

        # 未提供文件名时, 根据 MIME 类型推断扩展名。
        if not filename:
            ext = mimetypes.guess_extension(self.mimetype)
            if not ext:
                if self.mimetype == "image/png":
                    ext = ".png"
                elif self.mimetype == "image/jpeg":
                    ext = ".jpg"
                elif self.mimetype == "image/gif":
                    ext = ".gif"
                else:
                    ext = ".bin"
            self.filename = f"upload_{uuid.uuid4().hex[:8]}{ext}"
        elif "." not in filename:
            ext = mimetypes.guess_extension(self.mimetype) or ".bin"
            self.filename = f"{filename}{ext}"
        else:
            self.filename = filename

    def decoded_bytes(self, max_bytes: int) -> bytes:
        """解码载荷, 拒绝超大或非法的输入内容."""
        # 在分配解码内存之前拒绝过大的编码输入。
        if len(self._data) > 4 * ((max_bytes + 2) // 3):
            message = "File too large"
            raise PayloadTooLargeError(message)
        try:
            file_data = base64.b64decode(self._data, validate=True)
        except (ValueError, binascii.Error) as exc:
            message = "Invalid Base64 upload"
            raise BusinessRuleError(message) from exc
        if len(file_data) > max_bytes:
            message = "File too large"
            raise PayloadTooLargeError(message)
        return file_data

    def save(self, destination: str, max_bytes: int = FILE_LIMIT) -> None:
        """解码并最多写入 ``max_bytes`` 字节到 ``destination``."""
        file_data = self.decoded_bytes(max_bytes)
        with Path(destination).open("wb") as handle:
            handle.write(file_data)

    @property
    def content_type(self) -> str:
        """返回检测到的 MIME 类型."""
        return self.mimetype


UploadInput: TypeAlias = (
    FastAPIUploadAdapter | Base64UploadAdapter | FileStorage | LegacyDiskUpload
)


def save_bounded(upload: UploadInput, destination: str, max_bytes: int) -> None:
    """持久化有界上传; 受信任的遗留对象保存整个文件."""
    if isinstance(upload, (FastAPIUploadAdapter, Base64UploadAdapter)):
        upload.save(destination, max_bytes=max_bytes)
    elif isinstance(upload, FileStorage):
        try:
            upload.stream.seek(0)
            with Path(destination).open("wb") as target:
                copy_bounded(upload.stream, target, max_bytes)
        except BaseException:
            remove_partial_safely(destination)
            raise
    else:
        # 遗留同步调用方提供自己的磁盘支持上传对象。
        upload.save(destination)
        if Path(destination).stat().st_size > max_bytes:
            remove_partial_safely(destination)
            message = "File too large"
            raise PayloadTooLargeError(message)


def copy_upload_bounded(
    upload: UploadInput, target: BinaryIO, max_bytes: int,
) -> None:
    """复制内容到自有句柄; 遗留路径适配器使用私有暂存目录."""
    if isinstance(upload, FastAPIUploadAdapter):
        upload.upload_file.file.seek(0)
        copy_bounded(upload.upload_file.file, target, max_bytes)
    elif isinstance(upload, FileStorage):
        upload.stream.seek(0)
        copy_bounded(upload.stream, target, max_bytes)
    elif isinstance(upload, Base64UploadAdapter):
        target.write(upload.decoded_bytes(max_bytes))
    else:
        with tempfile.TemporaryDirectory(
            prefix="skycloud-chunk-", ignore_cleanup_errors=True,
        ) as stage:
            path = str(Path(stage) / "upload")
            save_bounded(upload, path, max_bytes)
            with Path(path).open("rb") as source:
                copy_bounded(source, target, max_bytes)
