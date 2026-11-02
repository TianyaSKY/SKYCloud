"""OpenCode HTTP 服务器轻量异步客户端.

此处仅包含提供商调用. 响应解析归属于 ``opencode_events.py``,
专家编排归属于 ``ExpertEngine``.
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

    from runtime_common.json_types import JsonObject, JsonValue

HTTP_BAD_REQUEST = 400
HTTP_NO_CONTENT = 204
HTTP_NOT_FOUND = 404


class OpenCodeClientError(RuntimeError):
    """提供商调用失败或返回不可用的负载."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """记录提供商消息及其返回的 HTTP 状态码."""
        super().__init__(message)
        self.status_code = status_code


class OpenCodeActionError(OpenCodeClientError):
    """显式控制操作未获提供商确认."""


class _SseBuffer:
    """累积 SSE 字段直至空行完成一条记录."""

    def __init__(self) -> None:
        """启动空记录."""
        self._event_name: str | None = None
        self._data_lines: list[str] = []

    def feed(self, line: str) -> JsonObject | None:
        """消费单行原始数据, 当记录完成时返回."""
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
        """返回挂起的记录 (若存在) 并重置缓冲区."""
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
    """解码单条缓冲记录, 忽略非对象类型的提供商负载."""
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
    """返回提供商列表响应中的对象成员."""
    if not isinstance(result, list) or not all(
        isinstance(item, dict) for item in result
    ):
        message = "OpenCode returned invalid list response"
        raise OpenCodeClientError(message)
    return [item for item in result if isinstance(item, dict)]


class OpenCodeClient:
    """内部 OpenCode HTTP API 的类型化异步客户端."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float | None = None,
    ) -> None:
        """绑定单运行时基地址及其派生的 Basic 凭据."""
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
        """返回当前客户端用于 ``async with`` 上下文."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """释放提供商连接池."""
        await self.aclose()

    async def aclose(self) -> None:
        """释放底层 HTTP 连接池."""
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
        """返回提供商健康检查文档."""
        result = await self._json("GET", "/global/health")
        if not isinstance(result, dict) or not isinstance(
            result.get("healthy"), bool,
        ):
            message = "OpenCode returned invalid health response"
            raise OpenCodeClientError(message)
        return result

    async def create_session(self, title: str | None = None) -> str:
        """创建单个提供商会话并返回其标识符."""
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
        """返回单个提供商会话, 若不存在则返回 ``None``."""
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
        """发送单个提示词, 可选仅持久化上下文而不回复."""
        payload: JsonObject = {
            "parts": [{"type": "text", "text": text}],
            "agent": agent,
            "noReply": no_reply,
        }
        if system:
            payload["system"] = system
        # noReply 不进入模型循环且不发射空闲事件. 使用同步持久化,
        # 确保后续提示词不会反超上下文历史.
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
        """中止单个提供商会话并报告确认结果."""
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
        """以一次性决策响应单个权限请求."""
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
        """返回单条会话消息的提供商 diff 变更."""
        params: JsonValue = {"messageID": message_id} if message_id else None
        result = await self._json(
            "GET",
            f"/session/{self._path_value(session_id)}/diff",
            params=params,
        )
        return _object_list(result)

    async def get_messages(self, session_id: str) -> list[JsonObject]:
        """返回单个会话的提供商消息历史."""
        result = await self._json(
            "GET", f"/session/{self._path_value(session_id)}/message",
        )
        return _object_list(result)

    async def get_mcp_status(self) -> JsonObject:
        """返回提供商 MCP 服务器状态文档."""
        result = await self._json("GET", "/mcp")
        if not isinstance(result, dict):
            message = "OpenCode returned invalid MCP response"
            raise OpenCodeClientError(message)
        return result

    async def add_mcp(self, name: str, config: JsonObject) -> JsonObject:
        """注册单个 MCP 服务器并返回提供商响应."""
        result = await self._json(
            "POST", "/mcp", json={"name": name, "config": config},
        )
        if not isinstance(result, dict):
            message = "OpenCode returned invalid MCP response"
            raise OpenCodeClientError(message)
        return result

    async def _open_event_stream(self, path: str) -> httpx.Response:
        """针对单个提供商路径打开流式 SSE 请求."""
        request = self._client.build_request(
            "GET",
            path,
            headers={"Accept": "text/event-stream"},
            timeout=None,
        )
        return await self._client.send(request, stream=True)

    async def stream_events(self) -> AsyncIterator[JsonObject]:
        """从 ``/event`` 产出解码后的 SSE 记录.

        少数较早的 OpenCode 构建在 ``/global/event`` 暴露全局流;
        在遇到 404 时重试该路径可在滚动升级期间保持客户端可用.
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
