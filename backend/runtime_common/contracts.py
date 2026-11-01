"""Fixed data contracts shared by the application and Runtime Controller."""

from typing import TypedDict


class ContainerPayload(TypedDict):
    """The bounded container identity exposed over Controller RPC."""

    id: str
    name: str
    status: str
    image: str | None
    labels: dict[str, str]
    ports: dict[str, list[dict[str, str]] | None]


class UserSnapshotRow(TypedDict):
    """One managed Runtime identity returned for a single user."""

    id: str
    workspace_id: int
    user_id: int
    runtime_id: int
    status: str
