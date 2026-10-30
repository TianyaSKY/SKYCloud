"""Small async client for the OpenCode HTTP server.

Only provider calls live here.  Response interpretation belongs to
``opencode_events.py`` and expert orchestration belongs to ``ExpertEngine``.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Self, cast
from urllib.parse import quote

import httpx
from runtime_controller.client import auth_headers as controller_auth_headers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from runtime_controller.json_types import JsonObject, JsonValue

HTTP_BAD_REQUEST = 400
HTTP_NO_CONTENT = 204
HTTP_NOT_FOUND = 404


class OpenCodeClientError(RuntimeError):
    """A provider call failed or answered with an unusable payload."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Record the provider message and any HTTP status it answered with."""
        super().__init__(message)
        self.status_code = status_code


class OpenCodeActionError(OpenCodeClientError):
    """An explicit control action was not acknowledged by the provider."""


class _SseBuffer:
    """Accumulate SSE fields until a blank line completes one record."""

    def __init__(self) -> None:
        """Start an empty record."""
        self._event_name: str | None = None
        self._data_lines: list[str] = []

    def feed(self, line: str) -> JsonObject | None:
        """Consume one raw line, returning a record when one completes."""
        if line.startswith(":"):
            return None
        if line.startswith("event:"):
            self._event_name = line[6:].strip() or None
            return None
        if line.startswith("data:"):
            self._data_lines.append(line[5:].lstrip())
            return None
        if line.strip() or not self._data_lines:
            return None
        return self.flush()

    def flush(self) -> JsonObject | None:
        """Return the pending record, if any, and reset the buffer."""
        if not self._data_lines:
            return None
        record = _decode_sse_record(self._event_name, self._data_lines)
        self._event_name = None
        self._data_lines = []
        return record


def _decode_sse_record(
    event_name: str | None,
    data_lines: list[str],
) -> JsonObject | None:
    """Decode one buffered record, ignoring non-object provider payloads."""
    raw = "\n".join(data_lines)
    try:
        decoded: JsonValue = json.loads(raw)
    except (TypeError, ValueError):
        decoded = {"raw": raw}
    if not isinstance(decoded, dict):
        return None
    if event_name and "type" not in decoded:
        decoded["type"] = event_name
    decoded["event"] = event_name if event_name else decoded.get("event")
    return decoded


def _object_list(result: JsonValue) -> list[JsonObject]:
    """Return the object members of a provider list response."""
    if not isinstance(result, list) or not all(
        isinstance(item, dict) for item in result
    ):
        message = "OpenCode returned invalid list response"
        raise OpenCodeClientError(message)
    return [item for item in result if isinstance(item, dict)]


class OpenCodeClient:
    """Typed async client for the internal OpenCode HTTP API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float | None = None,
    ) -> None:
        """Bind one runtime base URL and its derived basic credentials."""
        self.base_url = base_url.rstrip("/")
        default_timeout = float(os.getenv("OPENCODE_HTTP_TIMEOUT", "30"))
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=httpx.BasicAuth(username, password),
            timeout=timeout if timeout is not None else default_timeout,
            headers={
                "Accept": "application/json",
                **controller_auth_headers(self.base_url),
            },
            trust_env=False,
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        """Return this client for ``async with`` use."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release the provider connection pool."""
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()

    @staticmethod
    def _path_value(value: str) -> str:
        return quote(str(value), safe="")

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json: JsonValue = None,
        params: JsonValue = None,
    ) -> JsonValue:
        try:
            response = await self._client.request(
                method,
                path,
                json=json,
                params=cast("dict[str, str] | None", params),
            )
        except httpx.HTTPError:
            message = "OpenCode request failed"
            raise OpenCodeClientError(message) from None
        if response.status_code >= HTTP_BAD_REQUEST:
            message = f"OpenCode returned HTTP {response.status_code}"
            raise OpenCodeClientError(message, status_code=response.status_code)
        if response.status_code == HTTP_NO_CONTENT:
            return True
        if not response.content:
            return None
        try:
            payload: JsonValue = response.json()
        except ValueError:
            message = "OpenCode returned invalid JSON"
            raise OpenCodeClientError(message) from None
        return payload

    async def health(self) -> JsonObject:
        """Return the provider health document."""
        result = await self._json("GET", "/global/health")
        if not isinstance(result, dict) or not isinstance(
            result.get("healthy"), bool,
        ):
            message = "OpenCode returned invalid health response"
            raise OpenCodeClientError(message)
        return result

    async def create_session(self, title: str | None = None) -> str:
        """Create one provider session and return its identifier."""
        payload: JsonObject = {"title": title} if title else {}
        result = await self._json("POST", "/session", json=payload)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("id"), str)
            or not result["id"]
        ):
            message = "OpenCode session response did not contain an id"
            raise OpenCodeClientError(message)
        return str(result["id"])

    async def get_session(self, session_id: str) -> JsonObject | None:
        """Return one provider session, or ``None`` when it is absent."""
        try:
            result = await self._json(
                "GET", f"/session/{self._path_value(session_id)}",
            )
        except OpenCodeClientError as exc:
            if exc.status_code == HTTP_NOT_FOUND:
                return None
            raise
        if not isinstance(result, dict) or result.get("id") != session_id:
            message = "OpenCode returned invalid session identity"
            raise OpenCodeClientError(message)
        return result

    async def prompt_async(
        self,
        session_id: str,
        text: str,
        agent: str,
        *,
        no_reply: bool = False,
        system: str | None = None,
    ) -> None:
        """Send one prompt, optionally persisting context without a reply."""
        payload: JsonObject = {
            "parts": [{"type": "text", "text": text}],
            "agent": agent,
            "noReply": no_reply,
        }
        if system:
            payload["system"] = system
        # noReply does not enter the model loop or emit an idle event. Use
        # synchronous persistence so the next prompt cannot overtake history.
        endpoint = "message" if no_reply else "prompt_async"
        result = await self._json(
            "POST",
            f"/session/{self._path_value(session_id)}/{endpoint}",
            json=payload,
        )
        if not no_reply:
            return
        info = result.get("info") if isinstance(result, dict) else None
        if (
            not isinstance(info, dict)
            or info.get("sessionID") != session_id
            or info.get("role") != "user"
            or not info.get("id")
        ):
            message = "OpenCode did not confirm persisted context"
            raise OpenCodeClientError(message)

    async def abort_session(self, session_id: str) -> bool:
        """Abort one provider session and report the acknowledgement."""
        return await self._control_action(
            f"/session/{self._path_value(session_id)}/abort",
        )

    async def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str,
        *,
        remember: bool = False,
    ) -> bool:
        """Answer one permission request with a one-shot decision."""
        if remember or response not in {"once", "reject"}:
            message = "Only one-shot permission responses are supported"
            raise ValueError(message)
        return await self._control_action(
            (
                f"/session/{self._path_value(session_id)}"
                f"/permissions/{self._path_value(permission_id)}"
            ),
            json={"response": response},
        )

    async def _control_action(
        self,
        path: str,
        *,
        json: JsonValue = None,
    ) -> bool:
        try:
            result = await self._json("POST", path, json=json)
        except OpenCodeClientError as error:
            message = "OpenCode control action was not confirmed"
            raise OpenCodeActionError(message, error.status_code) from None
        if result is True or (
            isinstance(result, dict) and result.get("success") is True
        ):
            return True
        message = "OpenCode control action was not confirmed"
        raise OpenCodeActionError(message)

    async def get_diff(
        self,
        session_id: str,
        message_id: str | None = None,
    ) -> list[JsonObject]:
        """Return the provider diffs for one session message."""
        params: JsonValue = {"messageID": message_id} if message_id else None
        result = await self._json(
            "GET",
            f"/session/{self._path_value(session_id)}/diff",
            params=params,
        )
        return _object_list(result)

    async def get_messages(self, session_id: str) -> list[JsonObject]:
        """Return the provider message history for one session."""
        result = await self._json(
            "GET", f"/session/{self._path_value(session_id)}/message",
        )
        return _object_list(result)

    async def get_mcp_status(self) -> JsonObject:
        """Return the provider MCP server status document."""
        result = await self._json("GET", "/mcp")
        if not isinstance(result, dict):
            message = "OpenCode returned invalid MCP response"
            raise OpenCodeClientError(message)
        return result

    async def add_mcp(self, name: str, config: JsonObject) -> JsonObject:
        """Register one MCP server and return the provider response."""
        result = await self._json(
            "POST", "/mcp", json={"name": name, "config": config},
        )
        if not isinstance(result, dict):
            message = "OpenCode returned invalid MCP response"
            raise OpenCodeClientError(message)
        return result

    async def _open_event_stream(self, path: str) -> httpx.Response:
        """Open a streaming SSE request against one provider path."""
        request = self._client.build_request(
            "GET",
            path,
            headers={"Accept": "text/event-stream"},
            timeout=None,
        )
        return await self._client.send(request, stream=True)

    async def stream_events(self) -> AsyncIterator[JsonObject]:
        """Yield decoded SSE records from ``/event``.

        A few older OpenCode builds exposed the global stream at
        ``/global/event``; retrying that path on a 404 keeps the client useful
        during a rolling runtime upgrade.
        """
        response: httpx.Response | None = None
        try:
            response = await self._open_event_stream("/event")
            if response.status_code == HTTP_NOT_FOUND:
                await response.aclose()
                response = await self._open_event_stream("/global/event")
            if response.status_code >= HTTP_BAD_REQUEST:
                message = (
                    f"OpenCode event stream returned HTTP {response.status_code}"
                )
                raise OpenCodeClientError(
                    message,
                    status_code=response.status_code,
                )
            buffer = _SseBuffer()
            async for line in response.aiter_lines():
                record = buffer.feed(line)
                if record is not None:
                    yield record
            final_record = buffer.flush()
            if final_record is not None:
                yield final_record
        except httpx.HTTPError:
            message = "OpenCode event stream failed"
            raise OpenCodeClientError(message) from None
        finally:
            if response is not None:
                await response.aclose()
