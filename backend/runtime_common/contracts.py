"""FastAPI 应用与 Runtime Controller 之间共享的固定数据契约."""

from typing import TypedDict


class ContainerPayload(TypedDict):
    """通过 Controller RPC 暴露的有界容器标识."""

    id: str
    name: str
    status: str
    image: str | None
    labels: dict[str, str]
    ports: dict[str, list[dict[str, str]] | None]


class UserSnapshotRow(TypedDict):
    """单个用户返回的托管 Runtime 实例快照."""

    id: str
    workspace_id: int
    user_id: int
    runtime_id: int
    status: str
