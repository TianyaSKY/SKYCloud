"""Use the standalone Controller codec with backend-owned reader cleanup."""
from runtime_controller.http_decode import ACCEPT_ENCODING, CHUNK_BYTES, content_encoding
from runtime_controller.http_decode import decoded_bytes as _decoded_bytes
from app.infra.async_stream import close_reader


def decoded_bytes(response, maximum):
    return _decoded_bytes(response, maximum, reader_close=close_reader)
