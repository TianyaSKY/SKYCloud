"""上传适配层：把 FastAPI UploadFile / Base64 统一成 service 期望的 save(path) 接口。

历史 service 按 Werkzeug FileStorage 风格编写；适配器避免改动业务层签名。
"""

import base64
import mimetypes
import re
import shutil
import uuid

from fastapi import UploadFile


class FastAPIUploadAdapter:
    """包装 UploadFile，提供 ``filename`` / ``mimetype`` / ``save``。"""

    def __init__(self, upload_file: UploadFile):
        self._upload_file = upload_file
        self.filename = upload_file.filename or ""
        self.mimetype = upload_file.content_type

    def save(self, destination: str) -> None:
        # 允许同一请求内多次 save，故先 seek 到起点
        self._upload_file.file.seek(0)
        with open(destination, "wb") as target:
            shutil.copyfileobj(self._upload_file.file, target)


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

    def save(self, destination: str) -> None:
        file_data = base64.b64decode(self._data)
        with open(destination, "wb") as f:
            f.write(file_data)

    @property
    def content_type(self):
        return self.mimetype
