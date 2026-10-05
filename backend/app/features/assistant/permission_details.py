"""Bounded display contract for a durable, one-shot permission request."""
from typing import Any


def permission_details(value: Any, permission_id: str | None) -> dict[str, str] | None:
    if not isinstance(permission_id, str) or not 0 < len(permission_id) <= 128:
        return None
    if not isinstance(value, dict) or value.get('permission_id') != permission_id:
        return None
    title = value.get('title')
    tool = value.get('tool')
    if not isinstance(title, str) or not title or len(title) > 2000:
        return None
    if tool is not None and (not isinstance(tool, str) or len(tool) > 4000):
        return None
    return {'permission_id': permission_id, 'title': title, **({'tool': tool} if tool else {})}
