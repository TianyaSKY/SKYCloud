"""Map OpenCode bus events to the stable SKYCloud AssistantEvent protocol."""

import json
from typing import Any

from app.features.assistant.event_protocol import AssistantEvent


def _properties(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("properties") or event.get("payload") or event.get("data")
    return value if isinstance(value, dict) else {}


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or event.get("event") or "").lower()


def _session_id(properties: dict[str, Any]) -> str | None:
    for key in ("sessionID", "sessionId", "session_id"):
        value = properties.get(key)
        if value:
            return str(value)
    for key in ("session", "info", "part", "permission"):
        nested = properties.get(key)
        if isinstance(nested, dict):
            value = _session_id(nested)
            if value:
                return value
    return None


def _tool_state(part: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    state = part.get("state") or properties.get("state")
    return state if isinstance(state, dict) else {}


def _permission_tool_label(value: Any) -> str | None:
    """Convert OpenCode's string/object permission details into safe text."""

    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name") or value.get("tool")
        details = value.get("command") or value.get("input") or value.get("args")
        if name and details is not None:
            detail_text = _permission_tool_label(details)
            if detail_text:
                return f"{name}: {detail_text}"
        for key in ("command", "path", "pattern", "description"):
            detail_text = _permission_tool_label(value.get(key))
            if detail_text:
                return detail_text
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:500]
        except (TypeError, ValueError):
            return str(value)
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
    elif line.startswith("**") and line.endswith("**"):
        title = line[2:-2].strip()
    elif line.startswith("__") and line.endswith("__"):
        title = line[2:-2].strip()
    else:
        return None
    if not title or len(title) > 160:
        return None
    return title


def _first_string(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _message_id(part: dict[str, Any], properties: dict[str, Any]) -> str | None:
    """Find the provider message id attached to a message part/event."""

    message_keys = ("messageID", "messageId", "message_id")
    for source in (part, properties):
        value = _first_string(source, message_keys)
        if value:
            return value
    for key in ("message", "info"):
        nested = properties.get(key)
        if isinstance(nested, dict):
            value = _first_string(nested, ("id", *message_keys))
            if value:
                return value
    return None


def _message_role(part: dict[str, Any], properties: dict[str, Any]) -> str | None:
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


def _remember_message_role(properties: dict[str, Any], text_state: dict[str, str]) -> None:
    """Cache roles from ``message.updated`` before its parts arrive."""

    candidates: list[dict[str, Any]] = [properties]
    for key in ("message", "info"):
        nested = properties.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        role = candidate.get("role")
        message_id = _first_string(candidate, ("id", "messageID", "messageId", "message_id"))
        if role and message_id:
            text_state[f"__message_role:{message_id}"] = str(role).lower()


def _is_user_text_part(
    part: dict[str, Any],
    properties: dict[str, Any],
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


def map_opencode_event(
    event: dict[str, Any],
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
    result: list[AssistantEvent] = []

    if kind in {"server.connected", "connected"}:
        result.append(AssistantEvent("status", {"content": "专家服务已连接"}))
        return result

    if "permission" in kind and any(token in kind for token in ("ask", "request", "update")):
        permission = properties.get("permission")
        permission = permission if isinstance(permission, dict) else properties
        permission_id = permission.get("id") or permission.get("permissionID") or permission.get("permissionId")
        if permission_id:
            tool = _permission_tool_label(permission.get("tool") or permission.get("command"))
            result.append(
                AssistantEvent(
                    "permission_required",
                    {
                        "permission_id": str(permission_id),
                        "title": permission.get("title") or permission.get("message") or "专家请求执行高风险操作",
                        "tool": tool,
                    },
                )
            )
        return result

    part = properties.get("part")
    part = part if isinstance(part, dict) else properties
    part_type = str(part.get("type") or "").lower()
    part_id = str(part.get("id") or part.get("partID") or "")

    if "part" in kind and part_type == "reasoning":
        # Expose only a concise heading as a task title; never stream the
        # provider's reasoning body into the user-visible answer.
        delta = properties.get("delta") or properties.get("textDelta") or part.get("delta")
        if isinstance(delta, str):
            reasoning_key = f"__reasoning:{part_id}" if part_id else "__reasoning"
            reasoning_text = text_state.get(reasoning_key, "") + delta
            text_state[reasoning_key] = reasoning_text
        else:
            reasoning_text = part.get("text")
            if not isinstance(reasoning_text, str):
                return result
        title = extract_reasoning_title(reasoning_text)
        if title and text_state.get("__title_emitted") != "1":
            text_state["__title_emitted"] = "1"
            result.append(AssistantEvent("title", {"content": title}))
        return result

    if "part" in kind and part_type == "text":
        delta = properties.get("delta") or properties.get("textDelta") or part.get("delta")
        text = delta if isinstance(delta, str) else part.get("text")
        if isinstance(text, str) and text:
            if delta is None and part_id:
                previous = text_state.get(part_id, "")
                text_state[part_id] = text
                if text.startswith(previous):
                    text = text[len(previous):]
            if text and not _is_user_text_part(part, properties, text, text_state):
                result.append(AssistantEvent("token", {"content": text}))
        return result

    if "part" in kind and part_type in {"tool", "tool_use", "tool-call", "tool_call"}:
        state = _tool_state(part, properties)
        status = str(
            part.get("status")
            or state.get("status")
            or properties.get("status")
            or "running"
        ).lower()
        if status in {"completed", "complete", "done", "success"}:
            event_name = "tool_completed"
        elif status in {"error", "failed", "failure"}:
            event_name = "tool_completed"
        else:
            event_name = "tool_started" if status in {"running", "pending"} else "tool_updated"
        result.append(
            AssistantEvent(
                event_name,
                {
                    "tool_call_id": str(part.get("callID") or part.get("callId") or part.get("id") or ""),
                    "tool": (
                        part.get("tool")
                        or part.get("name")
                        or state.get("tool")
                        or state.get("name")
                        or "OpenCode 工具"
                    ),
                    "status": status,
                },
            )
        )
        return result

    if kind in {"session.status", "session.updated", "session.idle", "session.busy"}:
        status = properties.get("status")
        status = status.get("type") if isinstance(status, dict) else status
        status = str(status or ("idle" if kind.endswith("idle") else "busy")).lower()
        if status in {"idle", "completed", "complete"} or kind.endswith(".idle"):
            result.append(AssistantEvent("status", {"content": "专家任务已完成", "status": "idle"}))
            result.append(AssistantEvent("done", {"status": "completed"}))
        else:
            result.append(AssistantEvent("status", {"content": "专家正在处理", "status": status}))
        return result

    if "error" in kind:
        result.append(
            AssistantEvent(
                "error",
                {"message": properties.get("message") or "OpenCode 执行失败"},
            )
        )
        return result

    return result
