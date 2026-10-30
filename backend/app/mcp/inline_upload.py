"""Bounded text/Base64 adaptation, independent of MCP context and SQL transactions.

Run on a worker: encoding and MIME inference can allocate/read synchronously.
"""

import base64
import binascii
import mimetypes

from app.infra.upload_adapter import Base64UploadAdapter

_MAX_FILENAME_LENGTH = 255
_TEXT_ENCODINGS = frozenset({"utf-8", "utf8", "text", "plain"})
_FILENAME_REQUIRED_MESSAGE = "filename is required"
_FILENAME_TOO_LONG_MESSAGE = "filename is too long"
_LIMIT_EXCEEDED_MESSAGE = "inline upload exceeds server limit"
_INVALID_DATA_URI_MESSAGE = "content contains an invalid data URI"
_INVALID_BASE64_MESSAGE = "content must be valid Base64"
_INVALID_ENCODING_MESSAGE = "content_encoding must be utf-8 or base64"


def _decode_text(content: str, max_bytes: int) -> bytes:
    """Decode plain text content, enforcing the byte budget twice."""
    if len(content) > max_bytes:
        raise ValueError(_LIMIT_EXCEEDED_MESSAGE)
    raw = content.encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError(_LIMIT_EXCEEDED_MESSAGE)
    return raw


def _decode_base64(content: str, encoded_limit: int) -> tuple[bytes, str | None]:
    """Decode Base64 content and report a data-URI media type when present."""
    compact = "".join((content or "").split())
    data_mime = None
    if compact.startswith("data:"):
        try:
            header, compact = compact.split(",", 1)
        except ValueError as exc:
            raise ValueError(_INVALID_DATA_URI_MESSAGE) from exc
        data_mime = header[5:].split(";", 1)[0].strip()
    if len(compact) > encoded_limit:
        raise ValueError(_LIMIT_EXCEEDED_MESSAGE)
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(_INVALID_BASE64_MESSAGE) from exc
    return raw, data_mime


def build_inline_upload(
    filename: str,
    content: str,
    content_encoding: str,
    mime_type: str | None,
    *,
    max_bytes: int,
) -> Base64UploadAdapter:
    """Build a bounded upload adapter from text or Base64 MCP content."""
    normalized_filename = (filename or "").strip()
    if not normalized_filename:
        raise ValueError(_FILENAME_REQUIRED_MESSAGE)
    if len(normalized_filename) > _MAX_FILENAME_LENGTH:
        raise ValueError(_FILENAME_TOO_LONG_MESSAGE)

    encoding = (content_encoding or "utf-8").strip().lower().replace("_", "-")
    encoded_limit = 4 * ((max_bytes + 2) // 3)
    if len(content) > encoded_limit + 1024:
        raise ValueError(_LIMIT_EXCEEDED_MESSAGE)

    guessed_mime = mimetypes.guess_type(normalized_filename)[0]
    if encoding in _TEXT_ENCODINGS:
        raw = _decode_text(content, max_bytes)
        effective_mime = mime_type or guessed_mime or "text/plain"
    elif encoding == "base64":
        raw, data_mime = _decode_base64(content, encoded_limit)
        if len(raw) > max_bytes:
            raise ValueError(_LIMIT_EXCEEDED_MESSAGE)
        effective_mime = (
            mime_type or data_mime or guessed_mime or "application/octet-stream"
        )
    else:
        raise ValueError(_INVALID_ENCODING_MESSAGE)

    if len(raw) > max_bytes:
        message = f"inline upload exceeds {max_bytes // (1024 * 1024)} MB"
        raise ValueError(message)

    adapter = Base64UploadAdapter(
        f"data:{effective_mime};base64,{base64.b64encode(raw).decode('ascii')}",
        filename=normalized_filename,
    )
    adapter.mimetype = effective_mime
    return adapter
