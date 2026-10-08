"""Bounded text/Base64 adaptation, independent of MCP context and SQL transactions.

Run on a worker: encoding and MIME inference can allocate/read synchronously.
"""
import base64
import binascii
import mimetypes
from app.infra.upload_adapter import Base64UploadAdapter


def build_inline_upload(
    filename: str,
    content: str,
    content_encoding: str,
    mime_type: str | None,
    *, max_bytes: int,
) -> Base64UploadAdapter:
    """Build a bounded upload adapter from text or Base64 MCP content."""

    normalized_filename = (filename or "").strip()
    if not normalized_filename:
        raise ValueError("filename is required")
    if len(normalized_filename) > 255:
        raise ValueError("filename is too long")

    encoding = (content_encoding or "utf-8").strip().lower().replace("_", "-")
    encoded_limit = 4 * ((max_bytes + 2) // 3)
    if len(content) > encoded_limit + 1024:
        raise ValueError("inline upload exceeds server limit")
    if encoding in {"utf-8", "utf8", "text", "plain"}:
        if len(content) > max_bytes:
            raise ValueError("inline upload exceeds server limit")
        raw = content.encode("utf-8")
        if len(raw) > max_bytes:
            raise ValueError("inline upload exceeds server limit")
        effective_mime = mime_type or mimetypes.guess_type(normalized_filename)[0] or "text/plain"
        encoded = base64.b64encode(raw).decode("ascii")
    elif encoding == "base64":
        compact = "".join((content or "").split())
        if compact.startswith("data:"):
            try:
                header, compact = compact.split(",", 1)
            except ValueError as exc:
                raise ValueError("content contains an invalid data URI") from exc
            data_mime = header[5:].split(";", 1)[0].strip()
        else:
            data_mime = None
        if len(compact) > encoded_limit:
            raise ValueError("inline upload exceeds server limit")
        try:
            raw = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content must be valid Base64") from exc
        if len(raw) > max_bytes:
            raise ValueError("inline upload exceeds server limit")
        effective_mime = mime_type or data_mime or mimetypes.guess_type(normalized_filename)[0]
        effective_mime = effective_mime or "application/octet-stream"
        encoded = base64.b64encode(raw).decode("ascii")
    else:
        raise ValueError("content_encoding must be utf-8 or base64")

    if len(raw) > max_bytes:
        raise ValueError(
            f"inline upload exceeds {max_bytes // (1024 * 1024)} MB"
        )

    adapter = Base64UploadAdapter(
        f"data:{effective_mime};base64,{encoded}",
        filename=normalized_filename,
    )
    adapter.mimetype = effective_mime
    return adapter
