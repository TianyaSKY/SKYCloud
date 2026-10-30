"""Decode negotiated upstream content without an unbounded output chunk."""

import asyncio
import zlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from typing import Final, Protocol, TypeAlias

import httpx

from runtime_controller.http_stream import AsyncClosableIterator, closable, close_reader

ACCEPT_ENCODING: Final = "gzip, deflate, identity"
CHUNK_BYTES: Final = 64 * 1024
IDENTITY_ENCODINGS: Final = frozenset({"", "identity"})
SUPPORTED_ENCODINGS: Final = frozenset({"", "identity", "gzip", "deflate"})
GZIP_WBITS: Final = 31
ZLIB_WBITS: Final = 15
RAW_WBITS: Final = -15
DEFLATE_HEADER_BYTES: Final = 2
ZLIB_METHOD_MASK: Final = 15
ZLIB_DEFLATE_METHOD: Final = 8
ZLIB_WINDOW_SHIFT: Final = 4
ZLIB_MAX_WINDOW_BITS: Final = 7
ZLIB_CHECK_MODULUS: Final = 31

ReaderClose: TypeAlias = Callable[
    [asyncio.Task[bytes] | None, AsyncClosableIterator[bytes]],
    Awaitable[bool],
]


class ZlibDecoder(Protocol):
    """The ``zlib.decompressobj`` surface used for bounded decoding."""

    @property
    def eof(self) -> bool:
        """Whether the compressed stream ended cleanly."""
        ...

    @property
    def unconsumed_tail(self) -> bytes:
        """Input not yet consumed by the last bounded decompression."""
        ...

    def decompress(
        self,
        data: bytes | bytearray | memoryview,
        /,
        max_length: int = 0,
    ) -> bytes:
        """Decompress one chunk, returning at most ``max_length`` bytes."""
        ...


def content_encoding(response: httpx.Response) -> str:
    """Return the negotiated encoding, rejecting anything unsupported."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in SUPPORTED_ENCODINGS:
        message = "Unsupported upstream content encoding"
        raise httpx.DecodingError(message)
    return encoding


def decoded_bytes(
    response: httpx.Response,
    maximum: int,
    *,
    reader_close: ReaderClose = close_reader,
) -> AsyncIterator[bytes]:
    """Return the decoded body stream, bounded by ``maximum`` output bytes."""
    encoding = content_encoding(response)
    if encoding in IDENTITY_ENCODINGS:
        return response.aiter_bytes()
    return compressed_bytes(response, maximum, encoding, reader_close)


def _check_complete(decoder: ZlibDecoder) -> None:
    """Require a clean stream end; a partial body is never a success."""
    if not decoder.eof:
        message = "Incomplete upstream compressed response"
        raise httpx.DecodingError(message)


def _deflate_decoder(data: bytes) -> tuple[ZlibDecoder, bytes]:
    """Choose the zlib or raw inflate decoder from the deflate header."""
    cmf, flg = data[:DEFLATE_HEADER_BYTES]
    wrapped = (
        cmf & ZLIB_METHOD_MASK == ZLIB_DEFLATE_METHOD
        and cmf >> ZLIB_WINDOW_SHIFT <= ZLIB_MAX_WINDOW_BITS
        and (cmf * 256 + flg) % ZLIB_CHECK_MODULUS == 0
    )
    return zlib.decompressobj(ZLIB_WBITS if wrapped else RAW_WBITS), data


class DecodeState:
    """Bounded decoder state for one compressed upstream body."""

    def __init__(self, maximum: int, encoding: str) -> None:
        """Start with the decoder implied by the negotiated encoding."""
        wbits = GZIP_WBITS if encoding == "gzip" else ZLIB_WBITS
        self.maximum = maximum
        self.decoder: ZlibDecoder = zlib.decompressobj(wbits)
        self.remaining = maximum
        self.wire_size = 0
        self.first = True
        self.prefix = b""

    def add_wire(self, size: int) -> None:
        """Account one wire chunk before any decompression."""
        self.wire_size += size
        if self.wire_size > self.maximum + CHUNK_BYTES:
            message = "Upstream encoded response too large"
            raise httpx.DecodingError(message)

    def feed(self, data: bytes) -> Iterator[bytes]:
        """Yield bounded decoded output for one wire chunk."""
        while True:
            limit = min(CHUNK_BYTES, self.remaining + 1)
            output = self._decompress(data, limit)
            self.first = False
            if len(output) > self.remaining:
                message = "Upstream decoded response too large"
                raise httpx.DecodingError(message)
            self.remaining -= len(output)
            if output:
                yield output
            data = self.decoder.unconsumed_tail
            if not data and len(output) < limit:
                return

    def _decompress(self, data: bytes, limit: int) -> bytes:
        try:
            return self.decoder.decompress(data, limit)
        except zlib.error:
            message = "Invalid upstream compressed response"
            raise httpx.DecodingError(message) from None


async def compressed_bytes(
    response: httpx.Response,
    maximum: int,
    encoding: str,
    reader_close: ReaderClose,
) -> AsyncGenerator[bytes, None]:
    """Decode a compressed body while enforcing the wire and output limits."""
    owned = closable(response.aiter_raw().__aiter__())
    state = DecodeState(maximum, encoding)
    failed = False
    try:
        async for raw in owned:
            state.add_wire(len(raw))
            data = raw
            if encoding == "deflate" and state.first:
                state.prefix += data
                if len(state.prefix) < DEFLATE_HEADER_BYTES:
                    continue
                state.decoder, data = _deflate_decoder(state.prefix)
                state.prefix = b""
            for output in state.feed(data):
                yield output
        # Do not flush without an output limit: incomplete trailers must fail,
        # rather than return a partial body as a successful decoded response.
        _check_complete(state.decoder)
    except BaseException:
        failed = True
        raise
    finally:
        cancelled = await reader_close(None, owned)
        if cancelled and not failed:
            raise asyncio.CancelledError
