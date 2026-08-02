"""Map OpenCode bus events to the stable SKYCloud AssistantEvent protocol."""

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


def map_opencode_event(
    event: dict[str, Any],
    session_id: str,
    text_state: dict[str, str],
) -> list[AssistantEvent]:
    """Return zero or more safe events for one provider bus record."""

    kind = _event_type(event)
    properties = _properties(event)
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
            result.append(
                AssistantEvent(
                    "permission_required",
                    {
                        "permission_id": str(permission_id),
                        "title": permission.get("title") or permission.get("message") or "专家请求执行高风险操作",
                        "tool": permission.get("tool") or permission.get("command"),
                    },
                )
            )
        return result

    part = properties.get("part")
    part = part if isinstance(part, dict) else properties
    part_type = str(part.get("type") or "").lower()
    part_id = str(part.get("id") or part.get("partID") or "")

    if "part" in kind and part_type in {"text", "reasoning"}:
        delta = properties.get("delta") or properties.get("textDelta") or part.get("delta")
        text = delta if isinstance(delta, str) else part.get("text")
        if isinstance(text, str) and text:
            if delta is None and part_id:
                previous = text_state.get(part_id, "")
                text_state[part_id] = text
                if text.startswith(previous):
                    text = text[len(previous):]
            if text:
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
