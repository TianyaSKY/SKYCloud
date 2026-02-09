import shutil
import base64
import re
import mimetypes
import uuid
import io

from fastapi import UploadFile


class FastAPIUploadAdapter:
    """Compatibility adapter for existing services expecting Werkzeug FileStorage."""

    def __init__(self, upload_file: UploadFile):
        self._upload_file = upload_file
        self.filename = upload_file.filename or ""
        self.mimetype = upload_file.content_type

    def save(self, destination: str) -> None:
        self._upload_file.file.seek(0)
        with open(destination, "wb") as target:
            shutil.copyfileobj(self._upload_file.file, target)


class Base64UploadAdapter:
    """Adapter for Base64 encoded file upload."""

    def __init__(self, base64_str: str, filename: str = None):
        self._base64_str = base64_str
        self.filename = filename
        self.mimetype = "application/octet-stream"
        self._data = None
        
        # Parse data URI scheme if present
        if "," in base64_str:
            header, data = base64_str.split(",", 1)
            self._data = data
            match = re.match(r"data:([^;]+);base64", header)
            if match:
                self.mimetype = match.group(1)
        else:
            self._data = base64_str

        # Ensure filename has extension if not provided
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
