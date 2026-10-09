"""Decode negotiated upstream content without allocating an unbounded output chunk."""
import asyncio
import zlib
import httpx
from app.infra.async_stream import close_reader

ACCEPT_ENCODING = 'gzip, deflate, identity'
CHUNK_BYTES = 64 * 1024


def content_encoding(response):
    encoding = response.headers.get('content-encoding', '').strip().lower()
    if encoding not in ('', 'identity', 'gzip', 'deflate'):
        raise httpx.DecodingError('Unsupported upstream content encoding')
    return encoding


def decoded_bytes(response, maximum):
    encoding = content_encoding(response)
    if encoding in ('', 'identity'):
        return response.aiter_bytes()
    return compressed_bytes(response, maximum, encoding)


async def compressed_bytes(response, maximum, encoding):
    iterator = response.aiter_raw().__aiter__()
    decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15)
    remaining = maximum
    wire_size = 0
    first = True
    prefix = b''
    failed = False
    try:
        async for raw in iterator:
            wire_size += len(raw)
            if wire_size > maximum + CHUNK_BYTES:
                raise httpx.DecodingError('Upstream encoded response too large')
            data = raw
            if encoding == 'deflate' and first:
                prefix += data
                if len(prefix) < 2:
                    continue
                data = prefix
                cmf, flg = data[:2]
                wrapped = cmf & 15 == 8 and cmf >> 4 <= 7 and (cmf * 256 + flg) % 31 == 0
                decoder = zlib.decompressobj(15 if wrapped else -15)
                prefix = b''
            while True:
                limit = min(CHUNK_BYTES, remaining + 1)
                try:
                    output = decoder.decompress(data, limit)
                except zlib.error:
                    raise httpx.DecodingError('Invalid upstream compressed response') from None
                first = False
                if len(output) > remaining:
                    raise httpx.DecodingError('Upstream decoded response too large')
                remaining -= len(output)
                if output:
                    yield output
                data = decoder.unconsumed_tail
                if not data and len(output) < limit:
                    break
        # Do not flush without an output limit: incomplete trailers must fail,
        # rather than return a partial body as a successful decoded response.
        if not decoder.eof:
            raise httpx.DecodingError('Incomplete upstream compressed response')
    except BaseException:
        failed = True
        raise
    finally:
        cancelled = await close_reader(None, iterator)
        if cancelled and not failed:
            raise asyncio.CancelledError()
