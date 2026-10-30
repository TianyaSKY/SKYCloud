"""Compress change events into organizer context text and affected entity ids."""

from collections import Counter
from collections.abc import Sequence
from typing import TypedDict

from app.models.file_change_event import FileChangeEvent

DEFAULT_SUMMARY_EVENT_LINES = 40


class EventSummary(TypedDict):
    """Aggregated event statistics handed to the organizer prompt."""

    summary_text: str
    changed_file_ids: list[int]
    changed_folder_ids: list[int]
    action_breakdown: dict[str, int]


def _event_value(
    event: FileChangeEvent | dict[str, int | float | str | None],
    key: str,
    default: float | str | None = None,
) -> int | float | str | None:
    """Read one field from either an ORM row or a plain mapping."""
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def summarize_events(
    events: Sequence[FileChangeEvent | dict[str, int | float | str | None]],
    *,
    total_count: int,
    from_event_id: int,
    to_event_id: int,
    max_lines: int = DEFAULT_SUMMARY_EVENT_LINES,
) -> EventSummary:
    """Summarize action distribution and sample rows up to `max_lines`."""
    action_counter: Counter[str] = Counter()
    changed_file_ids: set[int] = set()
    changed_folder_ids: set[int] = set()
    lines: list[str] = []

    for event in events:
        event_id = int(_event_value(event, "id", 0) or 0)
        entity_type = str(_event_value(event, "entity_type", "unknown"))
        entity_id = int(_event_value(event, "entity_id", 0) or 0)
        action = str(_event_value(event, "action", "update_meta"))
        old_parent_id = _event_value(event, "old_parent_id")
        new_parent_id = _event_value(event, "new_parent_id")
        old_name = _event_value(event, "old_name")
        new_name = _event_value(event, "new_name")

        action_counter[f"{entity_type}:{action}"] += 1
        if entity_type == "file" and entity_id > 0:
            changed_file_ids.add(entity_id)
        if entity_type == "folder" and entity_id > 0:
            changed_folder_ids.add(entity_id)
        if old_parent_id is not None:
            changed_folder_ids.add(int(old_parent_id))
        if new_parent_id is not None:
            changed_folder_ids.add(int(new_parent_id))

        if len(lines) < max_lines:
            lines.append(
                f"- #{event_id} {entity_type}:{action} id={entity_id} "
                f"parent({old_parent_id}->{new_parent_id}) "
                f"name({old_name}->{new_name})",
            )

    breakdown_lines = [
        f"- {key}: {count}" for key, count in sorted(action_counter.items())
    ]
    summary_parts = [
        f"Event range: ({from_event_id}, {to_event_id}]",
        f"Total events: {total_count}",
        "Action breakdown:",
        "\n".join(breakdown_lines) if breakdown_lines else "- none",
        "Sample changes:",
        "\n".join(lines) if lines else "- none",
    ]

    return {
        "summary_text": "\n".join(summary_parts),
        "changed_file_ids": sorted(changed_file_ids),
        "changed_folder_ids": sorted(changed_folder_ids),
        "action_breakdown": dict(action_counter),
    }
