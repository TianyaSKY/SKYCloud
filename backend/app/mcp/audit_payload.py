"""有界的审计元数据. 绝不序列化参数体、凭证或具体对象."""

import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import ParamSpec, cast

from runtime_common.json_types import JsonObject, JsonValue

_P = ParamSpec("_P")

_MAX_SUMMARY_LENGTH = 1000
_NUMERIC_FIELDS = frozenset(
    {
        "id",
        "file_id",
        "folder_id",
        "parent_id",
        "page",
        "page_size",
        "max_depth",
        "max_chars",
        "expires_in",
    },
)
_ENTITY_FIELDS = ("file_id", "folder_id", "id")


@dataclass(frozen=True)
class AuditArguments:
    """脱敏调用摘要以及可能公开的单个实体 ID."""

    summary: str
    entity_id: int | None


def _shape(value: object, *, numeric: bool = False) -> JsonObject:
    """描述值的类型; 仅白名单内的数值字段保留具体取值."""
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "bool"}
    if type(value) is int:
        result: JsonObject = {"type": "int"}
        # 不序列化任意大整数或数值型凭证数据。
        if numeric and -(2**63) <= value < 2**63:
            result["value"] = value
        return result
    if isinstance(value, (str, bytes, bytearray)):
        kind = "str" if isinstance(value, str) else "bytes"
        return {"type": kind, "length": len(value)}
    if isinstance(value, (list, tuple, dict)):
        collection = cast("Sequence[object] | Mapping[object, object]", value)
        return {"type": "collection", "count": len(collection)}
    return {"type": "object"}


def summarize_call(
    func: Callable[_P, object],
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> AuditArguments:
    """使用声明的参数名; 无效或额外参数仅公开参数计数."""
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
        arguments = cast("dict[str, object]", dict(bound.arguments))
    except (TypeError, ValueError):
        return AuditArguments(
            json.dumps(
                {
                    "schema": 2,
                    "invalid_arguments": True,
                    "positional_count": len(args),
                    "keyword_count": len(kwargs),
                },
            ),
            None,
        )
    parameters: dict[str, JsonValue] = {
        name: _shape(value, numeric=name in _NUMERIC_FIELDS)
        for name, value in arguments.items()
    }
    entity_id = None
    for name in _ENTITY_FIELDS:
        value = arguments.get(name)
        if type(value) is int and 0 < value < 2**31:
            entity_id = value
            break
    if entity_id is None and isinstance(arguments.get("items"), (list, tuple)):
        items = cast("Sequence[object]", arguments["items"])[:10]
        for item in items:
            if isinstance(item, Mapping):
                value = cast("Mapping[str, object]", item).get("id")
            else:
                value = getattr(item, "id", None)
            if type(value) is int and 0 < value < 2**31:
                entity_id = value
                break
    payload: JsonObject = {"schema": 2, "parameters": parameters}
    summary = json.dumps(payload, ensure_ascii=False)
    if len(summary) > _MAX_SUMMARY_LENGTH:
        summary = json.dumps({"schema": 2, "parameter_count": len(arguments)})
    return AuditArguments(summary, entity_id)
