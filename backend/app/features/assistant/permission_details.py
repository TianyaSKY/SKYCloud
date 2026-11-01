"""Bounded display contract for a durable, one-shot permission request."""

from runtime_common.json_types import JsonValue

MAX_PERMISSION_ID_LENGTH = 128
MAX_TITLE_LENGTH = 2000
MAX_TOOL_LENGTH = 4000


def permission_details(
    value: JsonValue,
    permission_id: str | None,
) -> dict[str, str] | None:
    """Project provider permission metadata onto the bounded UI contract."""
    if not isinstance(permission_id, str) or not (
        0 < len(permission_id) <= MAX_PERMISSION_ID_LENGTH
    ):
        return None
    if not isinstance(value, dict) or value.get("permission_id") != permission_id:
        return None
    title = value.get("title")
    tool = value.get("tool")
    if not isinstance(title, str) or not title or len(title) > MAX_TITLE_LENGTH:
        return None
    if tool is not None and (
        not isinstance(tool, str) or len(tool) > MAX_TOOL_LENGTH
    ):
        return None
    return {
        "permission_id": permission_id,
        "title": title,
        **({"tool": tool} if tool else {}),
    }
