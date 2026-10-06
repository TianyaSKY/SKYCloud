"""Bounded audit metadata. Never serialize argument bodies, credentials or objects."""
from dataclasses import dataclass
import inspect
import json
from typing import Any, Callable

_NUMERIC_FIELDS = frozenset({"id", "file_id", "folder_id", "parent_id", "page", "page_size",
                             "max_depth", "max_chars", "expires_in"})
_ENTITY_FIELDS = ("file_id", "folder_id", "id")


@dataclass(frozen=True)
class AuditArguments:
    summary: str
    entity_id: int | None


def _shape(value: Any, *, numeric: bool = False) -> dict:
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "bool"}
    if type(value) is int:
        result = {"type": "int"}
        # Do not serialize arbitrary large integers or numeric credential values.
        if numeric and -(2**63) <= value < 2**63:
            result["value"] = value
        return result
    if isinstance(value, str):
        return {"type": "str", "length": len(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "bytes", "length": len(value)}
    if isinstance(value, (list, tuple, dict)):
        return {"type": "collection", "count": len(value)}
    return {"type": "object"}


def summarize_call(func: Callable, args: tuple, kwargs: dict) -> AuditArguments:
    """Use declared parameter names; invalid/extra arguments disclose only counts."""
    try:
        arguments = inspect.signature(func).bind(*args, **kwargs).arguments
    except (TypeError, ValueError):
        return AuditArguments(json.dumps({"schema": 2, "invalid_arguments": True,
            "positional_count": len(args), "keyword_count": len(kwargs)}), None)
    parameters = {name: _shape(value, numeric=name in _NUMERIC_FIELDS)
                  for name, value in arguments.items()}
    entity_id = None
    for name in _ENTITY_FIELDS:
        value = arguments.get(name)
        if type(value) is int and 0 < value < 2**31:
            entity_id = value
            break
    if entity_id is None and isinstance(arguments.get("items"), (list, tuple)):
        for item in arguments["items"][:10]:
            value = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
            if type(value) is int and 0 < value < 2**31:
                entity_id = value
                break
    payload = {"schema": 2, "parameters": parameters}
    summary = json.dumps(payload, ensure_ascii=False)
    if len(summary) > 1000:
        summary = json.dumps({"schema": 2, "parameter_count": len(arguments)})
    return AuditArguments(summary, entity_id)
