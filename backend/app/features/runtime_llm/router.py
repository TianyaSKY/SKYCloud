"""Bounded OpenAI-compatible chat endpoint; no arbitrary upstream URLs or headers."""

import json
import time
import asyncio
from contextlib import aclosing

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from app.features.runtime_llm import quota, tokens
from app.infra.extensions import get_session_factory
from app.infra.llm.config import get_chat_model_config
from app.infra.http_stream import UpstreamLease, UpstreamResponse

router = APIRouter()
MAX_BODY_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_STREAM_SECONDS = 300
ALLOWED_FIELDS = {
    "model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens",
    "temperature", "top_p", "stop", "tools", "tool_choice", "parallel_tool_calls",
    "response_format", "seed", "presence_penalty", "frequency_penalty",
}
RECHECK_SECONDS = 1.0


async def authorized_bytes(response, request, session_factory):
    """Revalidate during quiet waits and before releasing every upstream chunk."""
    pending = None
    iterator = response.aiter_bytes().__aiter__()
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(502, 'Model response timed out')
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            ready, _ = await asyncio.wait({pending}, timeout=min(RECHECK_SECONDS, remaining))
            await run_in_threadpool(_validate_current_credential, request, session_factory, touch=False)
            if not ready:
                continue
            try:
                part = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            if time.monotonic() >= deadline:
                raise HTTPException(502, 'Model response timed out')
            yield part
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await iterator.aclose()


def current_credential(request: Request, session_factory=Depends(get_session_factory)) -> dict:
    return _validate_current_credential(request, session_factory)


def _validate_current_credential(request, session_factory, *, touch=True):
    auth = request.headers.get("Authorization", "")
    scheme, _, raw = auth.partition(" ")
    payload = None
    if scheme.lower() == "bearer" and raw:
        with session_factory() as session:
            payload = (tokens.validate_token(session, raw) if touch
                       else tokens.validate_token(session, raw, touch=False))
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid runtime model credential")
    return payload


def normalize_body(body: bytes, credential: dict) -> dict:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(data, dict) or set(data) - ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail="Unsupported model request fields")
    if data.get("model") != credential["model"]:
        raise HTTPException(status_code=403, detail="Model is not allowed for this runtime")
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 256:
        raise HTTPException(status_code=400, detail="Invalid messages")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise HTTPException(status_code=400, detail="Invalid message")
        content = message.get("content")
        if isinstance(content, list):
            # URL/image/audio inputs have unbounded provider-side costs and
            # fetch behavior. This gateway permits text and tool calls only.
            if any(not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str) for part in content):
                raise HTTPException(status_code=400, detail="Only text content is supported")
        elif content is not None and not isinstance(content, str):
            raise HTTPException(status_code=400, detail="Invalid message content")
    if "stream" in data and type(data["stream"]) is not bool:
        raise HTTPException(status_code=400, detail="Invalid stream flag")
    maximum = tokens.positive_setting("RUNTIME_LLM_MAX_OUTPUT_TOKENS", 4096)
    if "max_tokens" in data and "max_completion_tokens" in data:
        raise HTTPException(status_code=400, detail="Specify only one output token limit")
    field = "max_completion_tokens" if "max_completion_tokens" in data else "max_tokens"
    requested = data.get(field, maximum)
    if type(requested) is not int or not 1 <= requested <= maximum:
        raise HTTPException(status_code=400, detail="Output token limit exceeded")
    data[field] = requested
    return data


def upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), trust_env=False, follow_redirects=False)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, credential: dict = Depends(current_credential),
                           session_factory=Depends(get_session_factory)):
    chunks = bytearray()
    async for part in request.stream():
        if len(chunks) + len(part) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Model request too large")
        chunks.extend(part)
    data = normalize_body(bytes(chunks), credential)
    output_tokens = data.get("max_completion_tokens", data.get("max_tokens"))
    # Charge framing/message metadata as well as text; no reservation refund
    # on upstream failure or disconnect, which would permit quota bypass.
    await run_in_threadpool(quota.reserve, credential, len(chunks) + 1024, output_tokens)
    await run_in_threadpool(current_credential, request, session_factory)
    config = get_chat_model_config()
    if not config["key"] or any(char in config["key"] for char in "\r\n"):
        raise HTTPException(status_code=503, detail="Model provider unavailable")
    client = upstream_client()
    lease = UpstreamLease(client)
    response = None
    try:
        upstream_request = client.build_request(
            "POST", config["api"].rstrip("/") + "/chat/completions", json=data,
            headers={"Authorization": f"Bearer {config['key']}", "Content-Type": "application/json"},
        )
        response = await client.send(upstream_request, stream=True)
        lease.upstream = response
        if response.status_code >= 300:
            raise HTTPException(status_code=502, detail="Model provider rejected request")
        if not data.get("stream", False):
            result = bytearray()
            async with aclosing(authorized_bytes(response, request, session_factory)) as parts:
                async for part in parts:
                    if len(result) + len(part) > MAX_RESPONSE_BYTES:
                        raise HTTPException(status_code=502, detail="Model response too large")
                    result.extend(part)
            return Response(content=bytes(result), media_type="application/json")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Model provider unavailable") from exc
    finally:
        if response is None or not data.get("stream", False) or response.status_code >= 300:
            await lease.close()

    async def events():
        size = 0
        deadline = time.monotonic() + MAX_STREAM_SECONDS
        try:
            async with aclosing(authorized_bytes(response, request, session_factory)) as parts:
                async for part in parts:
                    size += len(part)
                    if size > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                        break
                    yield part
        except (httpx.HTTPError, HTTPException):
            # Never inject provider exception text into a user's SSE stream.
            return
        finally:
            await lease.close()

    return UpstreamResponse(events(), lease=lease, media_type="text/event-stream", headers={"Cache-Control": "no-store"})
