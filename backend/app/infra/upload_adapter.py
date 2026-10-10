"""上传适配层：把 FastAPI UploadFile / Base64 统一成 service 期望的 save(path) 接口。

历史 service 按 Werkzeug FileStorage 风格编写；适配器避免改动业务层签名。
"""

import base64
import mimetypes
import re
import os

from app.exceptions import BusinessRuleError, PayloadTooLargeError
from app.infra.upload_limits import FILE_LIMIT, copy_bounded
from app.infra.upload_cleanup import remove_partial_safely
import uuid

from fastapi import UploadFile
from werkzeug.datastructures import FileStorage


class FastAPIUploadAdapter:
    """包装 UploadFile，提供 ``filename`` / ``mimetype`` / ``save``。"""

    def __init__(self, upload_file: UploadFile):
        self._upload_file = upload_file
        self.filename = upload_file.filename or ""
        self.mimetype = upload_file.content_type

    def save(self, destination: str, max_bytes: int = FILE_LIMIT) -> None:
        # 允许同一请求内多次 save，故先 seek 到起点
        self._upload_file.file.seek(0)
        try:
            with open(destination, "wb") as target:
                copy_bounded(self._upload_file.file, target, max_bytes)
        except BaseException:
            remove_partial_safely(destination)
            raise


class Base64UploadAdapter:
    """解析 data URI 或裸 Base64，补全扩展名后写入磁盘。"""

    def __init__(self, base64_str: str, filename: str = None):
        self._base64_str = base64_str
        self.filename = filename
        self.mimetype = "application/octet-stream"
        self._data = None

        # 支持 data:image/png;base64,xxxx 形式
        if "," in base64_str:
            header, data = base64_str.split(",", 1)
            self._data = data
            match = re.match(r"data:([^;]+);base64", header)
            if match:
                self.mimetype = match.group(1)
        else:
            self._data = base64_str

        # 无文件名时按 MIME 猜扩展名，避免落盘无后缀
        if not self.filename:
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
        elif "." not in self.filename:
            ext = mimetypes.guess_extension(self.mimetype) or ".bin"
            self.filename = f"{self.filename}{ext}"

    def save(self, destination: str, max_bytes: int = FILE_LIMIT) -> None:
        # Reject oversized encoded input before allocating decoded bytes.
        if len(self._data) > 4 * ((max_bytes + 2) // 3):
            raise PayloadTooLargeError("File too large")
        try:
            file_data = base64.b64decode(self._data, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise BusinessRuleError("Invalid Base64 upload") from exc
        if len(file_data) > max_bytes:
            raise PayloadTooLargeError("File too large")
        with open(destination, "wb") as f:
            f.write(file_data)

    @property
    def content_type(self):
        return self.mimetype


def save_bounded(upload, destination: str, max_bytes: int) -> None:
    """Adapters from HTTP/MCP are bounded; retain trusted legacy save(path) support."""
    if isinstance(upload, (FastAPIUploadAdapter, Base64UploadAdapter)):
        upload.save(destination, max_bytes=max_bytes)
    elif isinstance(upload, FileStorage):
        try:
            upload.stream.seek(0)
            with open(destination, "wb") as target:
                copy_bounded(upload.stream, target, max_bytes)
        except BaseException:
            remove_partial_safely(destination)
            raise
    else:
        # Legacy synchronous callers provide their own disk-backed upload.
        upload.save(destination)
        if os.path.getsize(destination) > max_bytes:
            remove_partial_safely(destination)
            raise PayloadTooLargeError("File too large")
