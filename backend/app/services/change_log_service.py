import json
import logging
from datetime import datetime
from typing import Any

from app.extensions import db
from app.models.file_change_event import FileChangeEvent
from app.models.organize_checkpoint import OrganizeCheckpoint
from app.services.change_log_summary import summarize_events

logger = logging.getLogger(__name__)

DEFAULT_MAX_INCREMENTAL_EVENTS = 200


def _to_payload_text(payload: dict[str, Any] | str | None) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def log_event(
    *,
    user_id: int,
    entity_type: str,
    entity_id: int,
    action: str,
    old_parent_id: int | None = None,
    new_parent_id: int | None = None,
    old_name: str | None = None,
    new_name: str | None = None,
    payload: dict[str, Any] | str | None = None,
) -> bool:
    return (
        log_events_batch(
            user_id,
            [
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "action": action,
                    "old_parent_id": old_parent_id,
                    "new_parent_id": new_parent_id,
                    "old_name": old_name,
                    "new_name": new_name,
                    "payload": payload,
                }
            ],
        )
        > 0
    )


def log_events_batch(user_id: int, events: list[dict[str, Any]]) -> int:
    if not events:
        return 0

    rows = []
    for event in events:
        entity_id = event.get("entity_id")
        if entity_id is None:
            continue
        rows.append(
            FileChangeEvent(
                user_id=user_id,
                entity_type=str(event.get("entity_type") or "unknown"),
                entity_id=int(entity_id),
                action=str(event.get("action") or "update_meta"),
                old_parent_id=event.get("old_parent_id"),
                new_parent_id=event.get("new_parent_id"),
                old_name=event.get("old_name"),
                new_name=event.get("new_name"),
                payload=_to_payload_text(event.get("payload")),
            )
        )

    if not rows:
        return 0

    try:
        db.session.add_all(rows)
        db.session.commit()
        return len(rows)
    except Exception as exc:
        db.session.rollback()
        logger.warning(f"Failed to write change events for user {user_id}: {exc}")
        return 0


def get_latest_event_id(user_id: int) -> int:
    row = (
        FileChangeEvent.query.filter_by(user_id=user_id)
        .order_by(FileChangeEvent.id.desc())
        .first()
    )
    return int(row.id) if row else 0


def get_checkpoint_event_id(user_id: int) -> int:
    checkpoint = OrganizeCheckpoint.query.filter_by(user_id=user_id).first()
    if not checkpoint:
        return 0
    return int(checkpoint.last_event_id or 0)


def update_checkpoint(
    user_id: int, event_id: int, *, mark_full_scan: bool = False
) -> None:
    target_event_id = max(0, int(event_id or 0))
    checkpoint = OrganizeCheckpoint.query.filter_by(user_id=user_id).first()
    if checkpoint is None:
        checkpoint = OrganizeCheckpoint(user_id=user_id, last_event_id=target_event_id)
        if mark_full_scan:
            checkpoint.last_full_scan_at = datetime.utcnow()
        db.session.add(checkpoint)
    else:
        checkpoint.last_event_id = max(int(checkpoint.last_event_id or 0), target_event_id)
        if mark_full_scan:
            checkpoint.last_full_scan_at = datetime.utcnow()

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(f"Failed to update organize checkpoint for user {user_id}: {exc}")


def load_incremental_context(
    user_id: int, max_events: int = DEFAULT_MAX_INCREMENTAL_EVENTS
) -> dict[str, Any]:
    max_events = max(1, int(max_events or DEFAULT_MAX_INCREMENTAL_EVENTS))

    checkpoint = OrganizeCheckpoint.query.filter_by(user_id=user_id).first()
    has_checkpoint = checkpoint is not None
    checkpoint_event_id = int(checkpoint.last_event_id or 0) if checkpoint else 0
    target_event_id = get_latest_event_id(user_id)

    if target_event_id <= checkpoint_event_id:
        return {
            "has_changes": False,
            "has_checkpoint": has_checkpoint,
            "overflow": False,
            "checkpoint_event_id": checkpoint_event_id,
            "target_event_id": target_event_id,
            "total_events": 0,
            "events": [],
            "summary_text": "No file-system changes since last organize checkpoint.",
            "changed_file_ids": [],
            "changed_folder_ids": [],
            "action_breakdown": {},
        }

    query = (
        FileChangeEvent.query.filter(FileChangeEvent.user_id == user_id)
        .filter(FileChangeEvent.id > checkpoint_event_id)
        .filter(FileChangeEvent.id <= target_event_id)
        .order_by(FileChangeEvent.id.asc())
    )
    total_events = query.count()
    events = query.limit(max_events).all()
    overflow = total_events > max_events

    summary = summarize_events(
        events,
        total_count=total_events,
        from_event_id=checkpoint_event_id,
        to_event_id=target_event_id,
    )

    return {
        "has_changes": total_events > 0,
        "has_checkpoint": has_checkpoint,
        "overflow": overflow,
        "checkpoint_event_id": checkpoint_event_id,
        "target_event_id": target_event_id,
        "total_events": total_events,
        "events": events,
        "summary_text": summary["summary_text"],
        "changed_file_ids": summary["changed_file_ids"],
        "changed_folder_ids": summary["changed_folder_ids"],
        "action_breakdown": summary["action_breakdown"],
    }
