"""Bounded OpenAI-compatible chat endpoint; no arbitrary upstream URLs or headers."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from runtime_common.http_decode import ACCEPT_ENCODING, content_encoding, decoded_bytes
from runtime_common.http_stream import (
    UpstreamLease,
    UpstreamResponse,
    closable,
    close_reader,
)
from runtime_common.http_wait import WaitPolicy, guarded_wait
from runtime_common.json_types import JsonObject, JsonValue
from starlette.requests import ClientDisconnect

from app.features.runtime_llm import quota, tokens
from app.features.runtime_llm.tokens import RuntimeLlmClaims
from app.infra.extensions import SessionFactory, get_session_factory
from app.infra.llm.config import get_chat_model_config
from app.infra.owned_work import run_owned

router = APIRouter()
MAX_BODY_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_STREAM_SECONDS = 300
MAX_BODY_SECONDS = 30
MAX_HEADER_SECONDS = 60
MAX_MESSAGES = 256
REJECTED_UPSTREAM_STATUS = 300
ALLOWED_FIELDS = {
    "model", "messages", "stream", "stream_options", "max_tokens",
    "max_completion_tokens",
    "temperature", "top_p", "stop", "tools", "tool_choice", "parallel_tool_calls",
    "response_format", "seed", "presence_penalty", "frequency_penalty",
}
MESSAGE_ROLES = {"system", "developer", "user", "assistant", "tool"}
RECHECK_SECONDS = 1.0

INVALID_CREDENTIAL = "Invalid runtime model credential"
CLIENT_DISCONNECTED = "Model client disconnected"
RESPONSE_TIMED_OUT = "Model response timed out"
INVALID_JSON = "Invalid JSON"
UNSUPPORTED_FIELDS = "Unsupported model request fields"
MODEL_NOT_ALLOWED = "Model is not allowed for this runtime"
INVALID_MESSAGES = "Invalid messages"
INVALID_MESSAGE = "Invalid message"
TEXT_ONLY = "Only text content is supported"
INVALID_CONTENT = "Invalid message content"
INVALID_STREAM = "Invalid stream flag"
ONE_OUTPUT_LIMIT = "Specify only one output token limit"
OUTPUT_LIMIT_EXCEEDED = "Output token limit exceeded"
REQUEST_TOO_LARGE = "Model request too large"
REQUEST_TIMED_OUT = "Model request timed out"
PROVIDER_UNAVAILABLE = "Model provider unavailable"
PROVIDER_REJECTED = "Model provider rejected request"
PROVIDER_TIMED_OUT = "Model provider timed out"
RESPONSE_TOO_LARGE = "Model response too large"


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Provider payload plus the bounds already validated from it."""

    body: JsonObject
    output_tokens: int
    stream: bool


async def _next_chunk(iterator: AsyncIterator[bytes]) -> bytes:
    return await anext(iterator)


def _require_time(remaining: float) -> None:
    """Fail the response once its remaining time budget is exhausted."""
    if remaining <= 0:
        raise HTTPException(status_code=502, detail=RESPONSE_TIMED_OUT)


async def verify_connected(request: Request, session_factory: SessionFactory) -> None:
    """Reject a disconnected client before any further upstream work.

    Only called after the request body has been consumed. A receive probe
    during body ingestion could discard an unread upload chunk.
    """
    if await request.is_disconnected():
        raise HTTPException(status_code=499, detail=CLIENT_DISCONNECTED)
    await run_owned(
        _validate_current_credential,
        request,
        session_factory,
        touch=False,
    )


async def authorized_bytes(
    response: httpx.Response,
    request: Request,
    session_factory: SessionFactory,
) -> AsyncGenerator[bytes, None]:
    """Revalidate during quiet waits and before releasing every upstream chunk."""
    pending: asyncio.Task[bytes] | None = None
    iterator = closable(decoded_bytes(response, MAX_RESPONSE_BYTES).__aiter__())
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    failed = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            _require_time(remaining)
            if pending is None:
                pending = asyncio.create_task(_next_chunk(iterator))
            ready, _ = await asyncio.wait(
                {pending},
                timeout=min(RECHECK_SECONDS, remaining),
            )
            await verify_connected(request, session_factory)
            if not ready:
                continue
            try:
                part = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            _require_time(deadline - time.monotonic())
            yield part
    except BaseException:
        failed = True
        raise
    finally:
        cancelled = await close_reader(pending, iterator)
        if cancelled and not failed:
            raise asyncio.CancelledError


async def current_credential(
    request: Request,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> RuntimeLlmClaims:
    """Return the caller's live runtime model credential or reject the request."""
    return await run_owned(_validate_current_credential, request, session_factory)


def _validate_current_credential(
    request: Request,
    session_factory: SessionFactory,
    *,
    touch: bool = True,
) -> RuntimeLlmClaims:
    auth = request.headers.get("Authorization", "")
    scheme, _, raw = auth.partition(" ")
    payload = None
    if scheme.lower() == "bearer" and raw:
        with session_factory() as session:
            payload = (
                tokens.validate_token(session, raw)
                if touch
                else tokens.validate_token(session, raw, touch=False)
            )
    if payload is None:
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIAL)
    return payload


def _validate_message(message: JsonValue) -> None:
    if not isinstance(message, dict) or message.get("role") not in MESSAGE_ROLES:
        raise HTTPException(status_code=400, detail=INVALID_MESSAGE)
    content = message.get("content")
    if isinstance(content, list):
        # URL/image/audio inputs have unbounded provider-side costs and
        # fetch behavior. This gateway permits text and tool calls only.
        if any(
            not isinstance(part, dict)
            or part.get("type") != "text"
            or not isinstance(part.get("text"), str)
            for part in content
        ):
            raise HTTPException(status_code=400, detail=TEXT_ONLY)
    elif content is not None and not isinstance(content, str):
        raise HTTPException(status_code=400, detail=INVALID_CONTENT)


def _validate_messages(data: JsonObject) -> None:
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
        raise HTTPException(status_code=400, detail=INVALID_MESSAGES)
    for message in messages:
        _validate_message(message)


def _apply_output_limit(data: JsonObject) -> int:
    maximum = tokens.positive_setting("RUNTIME_LLM_MAX_OUTPUT_TOKENS", 4096)
    if "max_tokens" in data and "max_completion_tokens" in data:
        raise HTTPException(status_code=400, detail=ONE_OUTPUT_LIMIT)
    field = "max_completion_tokens" if "max_completion_tokens" in data else "max_tokens"
    requested = data.get(field, maximum)
    if type(requested) is not int or not 1 <= requested <= maximum:
        raise HTTPException(status_code=400, detail=OUTPUT_LIMIT_EXCEEDED)
    data[field] = requested
    return requested


def normalize_body(body: bytes, credential: RuntimeLlmClaims) -> ModelRequest:
    """Validate a provider payload and bound it before quota or upstream use."""
    try:
        parsed: JsonValue = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=INVALID_JSON) from exc
    if not isinstance(parsed, dict) or set(parsed) - ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail=UNSUPPORTED_FIELDS)
    data: JsonObject = parsed
    if data.get("model") != credential["model"]:
        raise HTTPException(status_code=403, detail=MODEL_NOT_ALLOWED)
    _validate_messages(data)
    stream = data.get("stream")
    if "stream" in data and type(stream) is not bool:
        raise HTTPException(status_code=400, detail=INVALID_STREAM)
    output_tokens = _apply_output_limit(data)
    return ModelRequest(body=data, output_tokens=output_tokens, stream=stream is True)


def upstream_client() -> httpx.AsyncClient:
    """Build the provider client; it never follows redirects or reads proxy env."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10),
        trust_env=False,
        follow_redirects=False,
    )


async def _read_body(request: Request) -> bytearray:
    chunks = bytearray()
    try:
        async for part in request.stream():
            if len(chunks) + len(part) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail=REQUEST_TOO_LARGE)
            chunks.extend(part)
    except ClientDisconnect as exc:
        raise HTTPException(status_code=499, detail=CLIENT_DISCONNECTED) from exc
    return chunks


async def _read_authorized_body(
    request: Request,
    session_factory: SessionFactory,
) -> bytearray:
    async def verify() -> None:
        await run_owned(
            _validate_current_credential,
            request,
            session_factory,
            touch=False,
        )

    return await guarded_wait(
        _read_body(request),
        WaitPolicy(
            check=verify,
            seconds=MAX_BODY_SECONDS,
            status=408,
            detail=REQUEST_TIMED_OUT,
            interval=RECHECK_SECONDS,
        ),
    )


async def _open_upstream(
    client: httpx.AsyncClient,
    request: httpx.Request,
    lease: UpstreamLease,
) -> httpx.Response:
    opened = await client.send(request, stream=True)
    # Capture ownership even if revocation wins before the waiter can
    # accept a completed response with headers.
    lease.upstream = opened
    return opened


async def _collect_completion(
    response: httpx.Response,
    request: Request,
    session_factory: SessionFactory,
) -> Response:
    result = bytearray()
    parts = authorized_bytes(response, request, session_factory)
    async with aclosing(parts) as stream:
        async for part in stream:
            if len(result) + len(part) > MAX_RESPONSE_BYTES:
                raise HTTPException(status_code=502, detail=RESPONSE_TOO_LARGE)
            result.extend(part)
    return Response(content=bytes(result), media_type="application/json")


def _streaming_completion(
    response: httpx.Response,
    lease: UpstreamLease,
    request: Request,
    session_factory: SessionFactory,
) -> UpstreamResponse:
    async def events() -> AsyncGenerator[bytes, None]:
        size = 0
        deadline = time.monotonic() + MAX_STREAM_SECONDS
        try:
            parts = authorized_bytes(response, request, session_factory)
            async with aclosing(parts) as stream:
                async for part in stream:
                    size += len(part)
                    if size > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                        break
                    yield part
        except (httpx.HTTPError, HTTPException):
            # Never inject provider exception text into a user's SSE stream.
            return
        finally:
            await lease.close()

    return UpstreamResponse(
        events(),
        lease=lease,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


async def _proxy_completion(
    request: Request,
    session_factory: SessionFactory,
    payload: ModelRequest,
    config: dict[str, str],
) -> Response:
    client = upstream_client()
    lease = UpstreamLease(client)
    accepted = False
    try:
        upstream_request = client.build_request(
            "POST",
            config["api"].rstrip("/") + "/chat/completions",
            json=payload.body,
            headers={
                "Authorization": f"Bearer {config['key']}",
                "Content-Type": "application/json",
                "Accept-Encoding": ACCEPT_ENCODING,
            },
        )

        async def verify_headers() -> None:
            await verify_connected(request, session_factory)

        response = await guarded_wait(
            _open_upstream(client, upstream_request, lease),
            WaitPolicy(
                check=verify_headers,
                seconds=MAX_HEADER_SECONDS,
                status=504,
                detail=PROVIDER_TIMED_OUT,
                interval=RECHECK_SECONDS,
                on_abort=lease.close,
            ),
        )
        if response.status_code >= REJECTED_UPSTREAM_STATUS:
            raise HTTPException(status_code=502, detail=PROVIDER_REJECTED)
        content_encoding(response)
        accepted = True
        if payload.stream:
            return _streaming_completion(response, lease, request, session_factory)
        return await _collect_completion(response, request, session_factory)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=PROVIDER_UNAVAILABLE) from exc
    finally:
        if not accepted or not payload.stream:
            await lease.close()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    credential: Annotated[RuntimeLlmClaims, Depends(current_credential)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> Response:
    """Proxy one bounded chat completion to the configured model provider."""
    chunks = await _read_authorized_body(request, session_factory)
    payload = normalize_body(bytes(chunks), credential)
    # Charge framing/message metadata as well as text; no reservation refund
    # on upstream failure or disconnect, which would permit quota bypass.
    await run_owned(
        quota.reserve,
        credential,
        len(chunks) + 1024,
        payload.output_tokens,
    )
    await current_credential(request, session_factory)
    config = get_chat_model_config()
    if not config["key"] or any(char in config["key"] for char in "\r\n"):
        raise HTTPException(status_code=503, detail=PROVIDER_UNAVAILABLE)
    return await _proxy_completion(request, session_factory, payload, config)
