"""Small async client for the OpenCode HTTP server.

Only provider calls live here.  Response interpretation belongs to
``opencode_events.py`` and expert orchestration belongs to ``ExpertEngine``.
"""

import json
import os
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx


class OpenCodeClientError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OpenCodeClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=httpx.BasicAuth(username, password),
            timeout=timeout if timeout is not None else float(os.getenv("OPENCODE_HTTP_TIMEOUT", "30")),
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "OpenCodeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _path_value(value: str) -> str:
        return quote(str(value), safe="")

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OpenCodeClientError(f"OpenCode request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:500]
            raise OpenCodeClientError(
                f"OpenCode returned HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OpenCodeClientError("OpenCode returned invalid JSON") from exc

    async def health(self) -> dict[str, Any]:
        result = await self._json("GET", "/global/health")
        return result if isinstance(result, dict) else {"healthy": bool(result)}

    async def create_session(self, title: str | None = None) -> str:
        payload = {"title": title} if title else {}
        result = await self._json("POST", "/session", json=payload)
        if not isinstance(result, dict) or not result.get("id"):
            raise OpenCodeClientError("OpenCode session response did not contain an id")
        return str(result["id"])

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            result = await self._json("GET", f"/session/{self._path_value(session_id)}")
        except OpenCodeClientError as exc:
            if exc.status_code == 404:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def prompt_async(
        self,
        session_id: str,
        text: str,
        agent: str,
        *,
        no_reply: bool = False,
        system: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": text}],
            "agent": agent,
            "noReply": no_reply,
        }
        if system:
            payload["system"] = system
        await self._json(
            "POST",
            f"/session/{self._path_value(session_id)}/prompt_async",
            json=payload,
        )

    async def abort_session(self, session_id: str) -> bool:
        result = await self._json("POST", f"/session/{self._path_value(session_id)}/abort")
        return self._result_ok(result)

    async def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str,
        remember: bool = False,
    ) -> bool:
        result = await self._json(
            "POST",
            f"/session/{self._path_value(session_id)}/permissions/{self._path_value(permission_id)}",
            json={"response": response, "remember": remember},
        )
        return self._result_ok(result)

    @staticmethod
    def _result_ok(result: Any) -> bool:
        if result is None or result is True:
            return True
        if isinstance(result, dict):
            return bool(result.get("success", True))
        return bool(result)

    async def get_diff(self, session_id: str, message_id: str | None = None) -> list[dict[str, Any]]:
        params = {"messageID": message_id} if message_id else None
        result = await self._json(
            "GET", f"/session/{self._path_value(session_id)}/diff", params=params
        )
        return result if isinstance(result, list) else []

    async def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        result = await self._json("GET", f"/session/{self._path_value(session_id)}/message")
        return result if isinstance(result, list) else []

    async def get_mcp_status(self) -> dict[str, Any]:
        result = await self._json("GET", "/mcp")
        return result if isinstance(result, dict) else {}

    async def add_mcp(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        result = await self._json("POST", "/mcp", json={"name": name, "config": config})
        return result if isinstance(result, dict) else {}

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded SSE records from ``/event``.

        A few older OpenCode builds exposed the global stream at
        ``/global/event``; retrying that path on a 404 keeps the client useful
        during a rolling runtime upgrade.
        """

        response: httpx.Response | None = None
        request = self._client.build_request(
            "GET", "/event", headers={"Accept": "text/event-stream"}, timeout=None
        )
        try:
            response = await self._client.send(request, stream=True)
            if response.status_code == 404:
                await response.aclose()
                request = self._client.build_request(
                    "GET", "/global/event", headers={"Accept": "text/event-stream"}, timeout=None
                )
                response = await self._client.send(request, stream=True)
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
                raise OpenCodeClientError(
                    f"OpenCode event stream returned HTTP {response.status_code}: {detail}",
                    status_code=response.status_code,
                )

            event_name: str | None = None
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip() or None
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line.strip() and data_lines:
                    raw = "\n".join(data_lines)
                    try:
                        data = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        data = {"raw": raw}
                    if isinstance(data, dict):
                        if event_name and "type" not in data:
                            data["type"] = event_name
                        data["event"] = event_name or data.get("event")
                        yield data
                    event_name = None
                    data_lines = []
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    data = {"raw": raw}
                if isinstance(data, dict):
                    if event_name and "type" not in data:
                        data["type"] = event_name
                    data["event"] = event_name or data.get("event")
                    yield data
        except httpx.HTTPError as exc:
            raise OpenCodeClientError(f"OpenCode event stream failed: {exc}") from exc
        finally:
            if response is not None:
                await response.aclose()
