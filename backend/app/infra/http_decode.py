"""Use the standalone Controller codec with backend-owned reader cleanup."""

from typing import cast

import httpx
from runtime_controller.http_decode import (
    ACCEPT_ENCODING,
    CHUNK_BYTES,
    content_encoding,
)
from runtime_controller.http_decode import decoded_bytes as _decoded_bytes

from app.infra.async_stream import CloseableAsyncIterator, close_reader


def decoded_bytes(
    response: httpx.Response,
    maximum: int,
) -> CloseableAsyncIterator[bytes]:
    """Decode a bounded upstream body while closing its reader on teardown."""
    return cast(
        "CloseableAsyncIterator[bytes]",
        _decoded_bytes(response, maximum, reader_close=close_reader),
    )


__all__ = ["ACCEPT_ENCODING", "CHUNK_BYTES", "content_encoding", "decoded_bytes"]
