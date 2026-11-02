"""内部助手事件与 HTTP SSE 编码器.

引擎发射这些稳定的事件, 而不是将提供商特有的负载泄漏给浏览器.
这样当 RAG 流水线或 OpenCode 独立演进时, 前端仍能保持兼容.
"""

import json
from dataclasses import dataclass, field
from typing import Any


def _empty_payload() -> dict[str, Any]:
    """返回全新的事件负载字典."""
    return {}


@dataclass(slots=True)
class AssistantEvent:
    """助手引擎发射的单个稳定事件."""

    type: str
    payload: dict[str, Any] = field(default_factory=_empty_payload)
    run_id: int | None = None
    message_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """为浏览器 SSE 契约序列化此事件."""
        result: dict[str, Any] = {
            "type": self.type,
            "payload": self.payload,
        }
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.message_id is not None:
            result["message_id"] = self.message_id
        return result


def encode_sse(event: AssistantEvent) -> str:
    """将单个内部事件编码为紧凑且浏览器安全的 SSE 记录."""
    return f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"


def heartbeat_event(run_id: int | None = None) -> AssistantEvent:
    """构建提供商空闲时发送的心跳保活事件."""
    return AssistantEvent(type="heartbeat", run_id=run_id)
