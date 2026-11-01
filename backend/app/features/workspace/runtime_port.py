"""Narrow RPC contract for workspace runtime lifecycle operations."""

from typing import Literal, Protocol

from runtime_common.contracts import ContainerPayload, UserSnapshotRow
from runtime_common.json_types import JsonObject


class RuntimeController(Protocol):
    """Operations available to application code through the Controller RPC."""

    def inspect(self, ref: str) -> ContainerPayload:
        """Inspect one managed Runtime by its exact identity or known reference."""
        ...

    def prepare(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
    ) -> ContainerPayload:
        """Prepare one generation-fenced Runtime candidate."""
        ...

    def retire_generation(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
    ) -> None:
        """Durably retire a generation before allowing its removal."""
        ...

    def remove_legacy(self, ref: str) -> None:
        """Remove one verified pre-generation Runtime identity."""
        ...

    def user_snapshot(self, user_id: int) -> list[UserSnapshotRow]:
        """List managed Runtime identities owned by one user."""
        ...

    def action(
        self,
        container_id: str,
        action: Literal["start", "stop", "restart", "remove"],
    ) -> None:
        """Run one bounded action on an exact managed container identity."""
        ...

    def read_config(self, container_id: str) -> JsonObject:
        """Read the normalized configuration of one managed Runtime."""
        ...

    def write_config(self, container_id: str, config: JsonObject) -> None:
        """Replace the normalized configuration of one managed Runtime."""
        ...
