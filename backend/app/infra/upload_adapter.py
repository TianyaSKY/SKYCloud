"""Upload adapters that expose the legacy ``save(path)`` contract to services.

Historical services were written for Werkzeug ``FileStorage`` style objects; the
adapters let FastAPI ``UploadFile`` and Base64 payloads keep that signature.
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
    """Legacy upload object that persists itself to a destination path."""

    def save(self, destination: str) -> None:
        """Write the upload to ``destination``."""


class FastAPIUploadAdapter:
    """Wrap an ``UploadFile`` as ``filename`` / ``mimetype`` / ``save``."""

    def __init__(self, upload_file: UploadFile) -> None:
        """Expose the bounded FastAPI upload under the legacy contract."""
        self.upload_file = upload_file
        self.filename = upload_file.filename or ""
        self.mimetype = upload_file.content_type

    def save(self, destination: str, max_bytes: int = FILE_LIMIT) -> None:
        """Copy at most ``max_bytes`` from the request into ``destination``."""
        # The same request may save more than once, so rewind to the start.
        self.upload_file.file.seek(0)
        try:
            with Path(destination).open("wb") as target:
                copy_bounded(self.upload_file.file, target, max_bytes)
        except BaseException:
            remove_partial_safely(destination)
            raise


class Base64UploadAdapter:
    """Parse a data URI or bare Base64 payload and add a file extension."""

    def __init__(self, base64_str: str, filename: str | None = None) -> None:
        """Decode the payload header and derive a safe file name."""
        self._base64_str = base64_str
        self.mimetype = "application/octet-stream"
        self._data: str

        # Support the data:image/png;base64,xxxx form.
        if "," in base64_str:
            header, data = base64_str.split(",", 1)
            self._data = data
            match = re.match(r"data:([^;]+);base64", header)
            if match:
                self.mimetype = match.group(1)
        else:
            self._data = base64_str

        # Without a filename, guess the extension from the MIME type.
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
        """Decode the payload, rejecting oversized or invalid input."""
        # Reject oversized encoded input before allocating decoded bytes.
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
        """Decode and write at most ``max_bytes`` into ``destination``."""
        file_data = self.decoded_bytes(max_bytes)
        with Path(destination).open("wb") as handle:
            handle.write(file_data)

    @property
    def content_type(self) -> str:
        """Return the detected MIME type."""
        return self.mimetype


UploadInput: TypeAlias = (
    FastAPIUploadAdapter | Base64UploadAdapter | FileStorage | LegacyDiskUpload
)


def save_bounded(upload: UploadInput, destination: str, max_bytes: int) -> None:
    """Persist a bounded upload; trusted legacy objects save the whole file."""
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
        # Legacy synchronous callers provide their own disk-backed upload.
        upload.save(destination)
        if Path(destination).stat().st_size > max_bytes:
            remove_partial_safely(destination)
            message = "File too large"
            raise PayloadTooLargeError(message)


def copy_upload_bounded(
    upload: UploadInput, target: BinaryIO, max_bytes: int,
) -> None:
    """Copy into an owned handle; legacy path adapters use a private stage."""
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
