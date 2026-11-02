"""解码协商的上游响应内容, 并严格限制输出分块大小."""

import asyncio
import zlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from typing import Final, Protocol, TypeAlias

import httpx

from runtime_common.async_stream import AsyncClosableIterator
from runtime_common.http_stream import closable, close_reader

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
    """用于有界解码的 ``zlib.decompressobj`` 接口规范."""

    @property
    def eof(self) -> bool:
        """压缩流是否已正常结束."""
        ...

    @property
    def unconsumed_tail(self) -> bytes:
        """尚未被最近一次有界解压消耗的输入尾部字节."""
        ...

    def decompress(
        self,
        data: bytes | bytearray | memoryview,
        /,
        max_length: int = 0,
    ) -> bytes:
        """解压单个数据分块, 至多返回 ``max_length`` 字节."""
        ...


def content_encoding(response: httpx.Response) -> str:
    """获取协商的内容编码方式, 拒绝任何不受支持的编码."""
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
    """返回解码后的响应体流, 受 ``maximum`` 输出字节上限约束."""
    encoding = content_encoding(response)
    if encoding in IDENTITY_ENCODINGS:
        return response.aiter_bytes()
    return compressed_bytes(response, maximum, encoding, reader_close)


def _check_complete(decoder: ZlibDecoder) -> None:
    """要求流正常完结; 不完整的响应体绝对不能视为成功."""
    if not decoder.eof:
        message = "Incomplete upstream compressed response"
        raise httpx.DecodingError(message)


def _deflate_decoder(data: bytes) -> tuple[ZlibDecoder, bytes]:
    """根据 deflate 头部特征选择 zlib 或 raw inflate 解码器."""
    cmf, flg = data[:DEFLATE_HEADER_BYTES]
    wrapped = (
        cmf & ZLIB_METHOD_MASK == ZLIB_DEFLATE_METHOD
        and cmf >> ZLIB_WINDOW_SHIFT <= ZLIB_MAX_WINDOW_BITS
        and (cmf * 256 + flg) % ZLIB_CHECK_MODULUS == 0
    )
    return zlib.decompressobj(ZLIB_WBITS if wrapped else RAW_WBITS), data


class DecodeState:
    """单个上游压缩响应体的有界解码器状态."""

    def __init__(self, maximum: int, encoding: str) -> None:
        """根据协商的编码方式初始化对应的解码器."""
        wbits = GZIP_WBITS if encoding == "gzip" else ZLIB_WBITS
        self.maximum = maximum
        self.decoder: ZlibDecoder = zlib.decompressobj(wbits)
        self.remaining = maximum
        self.wire_size = 0
        self.first = True
        self.prefix = b""

    def add_wire(self, size: int) -> None:
        """在执行任何解压前累计记录网络传输分块大小."""
        self.wire_size += size
        if self.wire_size > self.maximum + CHUNK_BYTES:
            message = "Upstream encoded response too large"
            raise httpx.DecodingError(message)

    def feed(self, data: bytes) -> Iterator[bytes]:
        """对单个网络分块生成有界解码输出."""
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
    """在强制执行网络大小和输出大小上限的同时解码压缩响应体."""
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
        # 禁止在没有输出限制的情况下 flush: 尾部不完整必须报错,
        # 而不能将部分响应体作为成功解码的响应返回。
        _check_complete(state.decoder)
    except BaseException:
        failed = True
        raise
    finally:
        cancelled = await reader_close(None, owned)
        if cancelled and not failed:
            raise asyncio.CancelledError
