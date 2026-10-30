"""Map OpenCode bus events to the stable SKYCloud AssistantEvent protocol."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.features.assistant.event_protocol import AssistantEvent

if TYPE_CHECKING:
    from runtime_controller.json_types import JsonObject, JsonValue

_CONNECTION_EVENTS = frozenset({"server.connected", "connected"})
_SESSION_EVENTS = frozenset(
    {"session.status", "session.updated", "session.idle", "session.busy"},
)
_TOOL_PART_TYPES = frozenset({"tool", "tool_use", "tool-call", "tool_call"})
_TERMINAL_TOOL_STATES = frozenset(
    {"completed", "complete", "done", "success", "error", "failed", "failure"},
)
_RUNNING_TOOL_STATES = frozenset({"running", "pending"})
_IDLE_SESSION_STATES = frozenset({"idle", "completed", "complete"})
_PERMISSION_KIND_TOKENS = ("ask", "request", "update")
_PERMISSION_DETAIL_KEYS = ("command", "path", "pattern", "description")
_MESSAGE_ID_KEYS = ("messageID", "messageId", "message_id")
_SESSION_ID_KEYS = ("sessionID", "sessionId", "session_id")
_SESSION_NESTED_KEYS = ("session", "info", "part", "permission")
_MAX_TITLE_LENGTH = 160
_PERMISSION_TITLE_LIMIT = 2000
_PERMISSION_TOOL_LIMIT = 4000
_TRUNCATION_SUFFIX = "…(内容已截断)"


def _properties(event: JsonObject) -> JsonObject:
    value = event.get("properties") or event.get("payload") or event.get("data")
    return value if isinstance(value, dict) else {}


def _event_type(event: JsonObject) -> str:
    return str(event.get("type") or event.get("event") or "").lower()


def _session_id(properties: JsonObject) -> str | None:
    for key in _SESSION_ID_KEYS:
        value = properties.get(key)
        if value:
            return str(value)
    for key in _SESSION_NESTED_KEYS:
        nested = properties.get(key)
        if isinstance(nested, dict):
            value = _session_id(nested)
            if value:
                return value
    return None


def _part(properties: JsonObject) -> JsonObject:
    part = properties.get("part")
    return part if isinstance(part, dict) else properties


def _tool_state(part: JsonObject, properties: JsonObject) -> JsonObject:
    state = part.get("state") or properties.get("state")
    return state if isinstance(state, dict) else {}


def _bounded_permission_text(value: str, limit: int) -> str:
    suffix = _TRUNCATION_SUFFIX
    return value if len(value) <= limit else value[: limit - len(suffix)] + suffix


def _permission_dict_label(value: JsonObject) -> str:
    name = value.get("name") or value.get("tool")
    details = value.get("command") or value.get("input") or value.get("args")
    if name and details is not None:
        detail_text = _permission_tool_label(details)
        if detail_text:
            return f"{name}: {detail_text}"
    for key in _PERMISSION_DETAIL_KEYS:
        detail_text = _permission_tool_label(value.get(key))
        if detail_text:
            return detail_text
    try:
        dumped = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)
    return _bounded_permission_text(dumped, _PERMISSION_TOOL_LIMIT)


def _permission_tool_label(value: JsonValue) -> str | None:
    """Convert OpenCode's string/object permission details into safe text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _permission_dict_label(value)
    if isinstance(value, (list, tuple)):
        items = [_permission_tool_label(item) for item in value]
        return ", ".join(item for item in items if item) or None
    return str(value)


def extract_reasoning_title(text: str) -> str | None:
    """Extract only a short provider heading, never the reasoning body."""
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    if not line:
        return None
    if line.startswith("#"):
        title = line.lstrip("#").strip()
    elif (line.startswith("**") and line.endswith("**")) or (
        line.startswith("__") and line.endswith("__")
    ):
        title = line[2:-2].strip()
    else:
        return None
    if not title or len(title) > _MAX_TITLE_LENGTH:
        return None
    return title


def _first_string(mapping: JsonObject, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _message_id(part: JsonObject, properties: JsonObject) -> str | None:
    """Find the provider message id attached to a message part/event."""
    for source in (part, properties):
        value = _first_string(source, _MESSAGE_ID_KEYS)
        if value:
            return value
    for key in ("message", "info"):
        nested = properties.get(key)
        if isinstance(nested, dict):
            value = _first_string(nested, ("id", *_MESSAGE_ID_KEYS))
            if value:
                return value
    return None


def _message_role(part: JsonObject, properties: JsonObject) -> str | None:
    """Read a provider message role from the common OpenCode payload shapes."""
    for source in (
        part,
        properties,
        properties.get("message"),
        properties.get("info"),
    ):
        if isinstance(source, dict):
            role = source.get("role")
            if role:
                return str(role).lower()
    return None


def _remember_message_role(
    properties: JsonObject,
    text_state: dict[str, str],
) -> None:
    """Cache roles from ``message.updated`` before its parts arrive."""
    candidates: list[JsonObject] = [properties]
    for key in ("message", "info"):
        nested = properties.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        role = candidate.get("role")
        message_id = _first_string(
            candidate, ("id", *_MESSAGE_ID_KEYS),
        )
        if role and message_id:
            text_state[f"__message_role:{message_id}"] = str(role).lower()


def _is_user_text_part(
    part: JsonObject,
    properties: JsonObject,
    text: str,
    text_state: dict[str, str],
) -> bool:
    """Keep provider user prompts out of the assistant answer stream.

    OpenCode normally gives us the role through ``message.updated``.  The
    prompt comparison is a defensive fallback for older runtimes that emit a
    part update without the preceding message metadata.
    """
    role = _message_role(part, properties)
    message_id = _message_id(part, properties)
    if role is None and message_id:
        role = text_state.get(f"__message_role:{message_id}")
    if role == "user":
        return True
    if role == "assistant":
        return False

    expected = text_state.get("__user_query")
    if not expected or text_state.get("__user_prompt_complete") == "1":
        return False
    buffer = text_state.get("__user_prompt_buffer", "")
    candidate = buffer + text
    if candidate == expected:
        text_state["__user_prompt_complete"] = "1"
        text_state.pop("__user_prompt_buffer", None)
        return True
    if expected.startswith(candidate):
        text_state["__user_prompt_buffer"] = candidate
        return True
    text_state.pop("__user_prompt_buffer", None)
    return False


def _is_permission_kind(kind: str) -> bool:
    return "permission" in kind and any(
        token in kind for token in _PERMISSION_KIND_TOKENS
    )


def _connection_events() -> list[AssistantEvent]:
    return [AssistantEvent("status", {"content": "专家服务已连接"})]


def _permission_labels(permission: JsonObject) -> tuple[str | None, str]:
    name = permission.get("permission")
    if isinstance(name, str):
        details = permission.get("metadata") or permission.get("patterns")
        if details:
            source: JsonObject = {"name": name, "input": details}
            tool = _permission_tool_label(source)
        else:
            tool = name
        # Current OpenCode tool is a message/call pointer; operation details
        # live in metadata and patterns, not in that pointer.
        title = (
            permission.get("title")
            or permission.get("message")
            or f"请求权限:{name}"
        )
    else:
        tool = _permission_tool_label(
            permission.get("tool") or permission.get("command"),
        )
        title = (
            permission.get("title")
            or permission.get("message")
            or "专家请求执行高风险操作"
        )
    return tool, str(title)


def _permission_events(
    properties: JsonObject,
    session_id: str,
    provider_session: str | None,
) -> list[AssistantEvent]:
    if provider_session != session_id:
        return []
    permission = properties.get("permission")
    permission = permission if isinstance(permission, dict) else properties
    permission_id = (
        permission.get("id")
        or permission.get("permissionID")
        or permission.get("permissionId")
    )
    if not permission_id:
        return []
    tool, title = _permission_labels(permission)
    return [
        AssistantEvent(
            "permission_required",
            {
                "permission_id": str(permission_id),
                "title": _bounded_permission_text(
                    title, _PERMISSION_TITLE_LIMIT,
                ),
                "tool": (
                    _bounded_permission_text(tool, _PERMISSION_TOOL_LIMIT)
                    if tool
                    else None
                ),
            },
        ),
    ]


def _reasoning_events(
    properties: JsonObject,
    part: JsonObject,
    part_id: str,
    text_state: dict[str, str],
) -> list[AssistantEvent]:
    # Expose only a concise heading as a task title; never stream the
    # provider's reasoning body into the user-visible answer.
    delta = properties.get("delta") or properties.get("textDelta") or part.get(
        "delta",
    )
    if isinstance(delta, str):
        reasoning_key = f"__reasoning:{part_id}" if part_id else "__reasoning"
        reasoning_text = text_state.get(reasoning_key, "") + delta
        text_state[reasoning_key] = reasoning_text
    else:
        fallback_text = part.get("text")
        if not isinstance(fallback_text, str):
            return []
        reasoning_text = fallback_text
    title = extract_reasoning_title(reasoning_text)
    if title and text_state.get("__title_emitted") != "1":
        text_state["__title_emitted"] = "1"
        return [AssistantEvent("title", {"content": title})]
    return []


def _text_events(
    properties: JsonObject,
    part: JsonObject,
    part_id: str,
    text_state: dict[str, str],
) -> list[AssistantEvent]:
    delta = properties.get("delta") or properties.get("textDelta") or part.get(
        "delta",
    )
    text = delta if isinstance(delta, str) else part.get("text")
    if not isinstance(text, str) or not text:
        return []
    if delta is None and part_id:
        previous = text_state.get(part_id, "")
        text_state[part_id] = text
        text = text.removeprefix(previous)
    if text and not _is_user_text_part(part, properties, text, text_state):
        return [AssistantEvent("token", {"content": text})]
    return []


def _tool_events(
    properties: JsonObject,
    part: JsonObject,
) -> list[AssistantEvent]:
    state = _tool_state(part, properties)
    status = str(
        part.get("status")
        or state.get("status")
        or properties.get("status")
        or "running",
    ).lower()
    if status in _TERMINAL_TOOL_STATES:
        event_name = "tool_completed"
    else:
        event_name = (
            "tool_started"
            if status in _RUNNING_TOOL_STATES
            else "tool_updated"
        )
    tool = (
        part.get("tool")
        or part.get("name")
        or state.get("tool")
        or state.get("name")
        or "OpenCode 工具"
    )
    call_id = str(
        part.get("callID") or part.get("callId") or part.get("id") or "",
    )
    return [
        AssistantEvent(
            event_name,
            {"tool_call_id": call_id, "tool": tool, "status": status},
        ),
    ]


def _session_events(
    kind: str,
    properties: JsonObject,
) -> list[AssistantEvent]:
    session_status = properties.get("status")
    if isinstance(session_status, dict):
        session_status = session_status.get("type")
    status = str(
        session_status or ("idle" if kind.endswith("idle") else "busy"),
    ).lower()
    if status in _IDLE_SESSION_STATES or kind.endswith(".idle"):
        return [
            AssistantEvent(
                "status", {"content": "专家任务已完成", "status": "idle"},
            ),
            AssistantEvent("done", {"status": "completed"}),
        ]
    return [
        AssistantEvent(
            "status", {"content": "专家正在处理", "status": status},
        ),
    ]


def _error_events(properties: JsonObject) -> list[AssistantEvent]:
    message = properties.get("message") or "OpenCode 执行失败"
    return [AssistantEvent("error", {"message": message})]


def _part_events(
    properties: JsonObject,
    text_state: dict[str, str],
) -> list[AssistantEvent] | None:
    part = _part(properties)
    part_type = str(part.get("type") or "").lower()
    part_id = str(part.get("id") or part.get("partID") or "")
    if part_type == "reasoning":
        return _reasoning_events(properties, part, part_id, text_state)
    if part_type == "text":
        return _text_events(properties, part, part_id, text_state)
    if part_type in _TOOL_PART_TYPES:
        return _tool_events(properties, part)
    return None


def _tail_events(
    kind: str,
    properties: JsonObject,
) -> list[AssistantEvent]:
    if kind in _SESSION_EVENTS:
        return _session_events(kind, properties)
    if "error" in kind:
        return _error_events(properties)
    return []


def map_opencode_event(
    event: JsonObject,
    session_id: str,
    text_state: dict[str, str],
) -> list[AssistantEvent]:
    """Return zero or more safe events for one provider bus record."""
    kind = _event_type(event)
    properties = _properties(event)
    _remember_message_role(properties, text_state)
    provider_session = _session_id(properties)
    if provider_session and provider_session != session_id:
        return []

    if kind in _CONNECTION_EVENTS:
        return _connection_events()
    if _is_permission_kind(kind):
        return _permission_events(properties, session_id, provider_session)
    if "part" in kind:
        part_events = _part_events(properties, text_state)
        if part_events is not None:
            return part_events
    return _tail_events(kind, properties)
