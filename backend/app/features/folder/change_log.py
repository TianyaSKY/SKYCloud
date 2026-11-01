"""文件/文件夹变更事件流水与整理检查点: 为增量整理提供上下文.

变更事件只做追加写入, 检查点只增不减; 两者共同决定下次整理是增量还是全量.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from loguru import logger
from runtime_common.json_types import JsonValue
from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from app.features.folder.change_log_summary import (
    EventSummary,
    summarize_events,
)
from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionLocal
from app.models.file import File
from app.models.file_change_event import FileChangeEvent
from app.models.folder import Folder
from app.models.organize_checkpoint import OrganizeCheckpoint

DEFAULT_MAX_INCREMENTAL_EVENTS = 200


class FileChangeDetail(TypedDict):
    """Current state of one file touched since the checkpoint."""

    id: int
    name: str
    parent_id: int | None
    path: str


class FolderChangeDetail(TypedDict):
    """Current state of one folder touched since the checkpoint."""

    id: int
    name: str
    parent_id: NotRequired[int | None]
    path: str


class ChangedDetails(TypedDict):
    """Resolved change details for prompt construction."""

    changed_files_detail: list[FileChangeDetail]
    changed_folders_detail: list[FolderChangeDetail]


class FileChangeEventSpec(TypedDict):
    """One change event to append to the log."""

    entity_type: str
    entity_id: int | None
    action: str
    old_parent_id: NotRequired[int | None]
    new_parent_id: NotRequired[int | None]
    old_name: NotRequired[str | None]
    new_name: NotRequired[str | None]
    payload: NotRequired[JsonValue]


class IncrementalContext(TypedDict):
    """Everything the organizer needs to decide incremental vs full scan."""

    has_changes: bool
    has_checkpoint: bool
    overflow: bool
    checkpoint_event_id: int
    target_event_id: int
    total_events: int | None
    events: list[FileChangeEvent]
    summary_text: str
    changed_file_ids: list[int]
    changed_folder_ids: list[int]
    action_breakdown: dict[str, int]
    changed_files_detail: list[FileChangeDetail]
    changed_folders_detail: list[FolderChangeDetail]


@dataclass(frozen=True, slots=True, kw_only=True)
class LogEventParams:
    """One change event described in the terms of the event log."""

    workspace_id: int
    entity_type: str
    entity_id: int
    action: str
    old_parent_id: int | None = None
    new_parent_id: int | None = None
    old_name: str | None = None
    new_name: str | None = None
    payload: JsonValue = None


def _build_folder_path(folder_id: int | None, folder_map: dict[int, Folder]) -> str:
    """Join the parent chain into a full path; cycle-safe for dirty data."""
    if folder_id is None or folder_id == 0:
        return "根目录"
    parts: list[str] = []
    current_id: int | None = folder_id
    seen: set[int] = set()
    while current_id and current_id in folder_map:
        if current_id in seen:
            break
        seen.add(current_id)
        folder = folder_map[current_id]
        parts.append(folder.name)
        current_id = folder.parent_id
    parts.reverse()
    return "根目录/" + "/".join(parts) if parts else "根目录"


def _resolve_changed_details(
    session: Session,
    workspace_id: int,
    changed_file_ids: list[int],
    changed_folder_ids: list[int],
) -> ChangedDetails:
    """Attach names and full paths to the incremental summary."""
    all_folders = session.query(Folder).filter_by(workspace_id=workspace_id).all()
    folder_map: dict[int, Folder] = {folder.id: folder for folder in all_folders}

    changed_files_detail: list[FileChangeDetail] = []
    if changed_file_ids:
        files = (
            session.query(File)
            .filter(
                File.id.in_(changed_file_ids),
                File.workspace_id == workspace_id,
            )
            .all()
        )
        changed_files_detail = [
            FileChangeDetail(
                id=file_obj.id,
                name=file_obj.name,
                parent_id=file_obj.parent_id,
                path=_build_folder_path(file_obj.parent_id, folder_map),
            )
            for file_obj in files
        ]

    changed_folders_detail: list[FolderChangeDetail] = []
    if changed_folder_ids:
        for folder_id in changed_folder_ids:
            if folder_id == 0:
                changed_folders_detail.append(
                    {"id": 0, "name": "根目录", "path": "根目录"},
                )
            elif folder_id in folder_map:
                folder = folder_map[folder_id]
                changed_folders_detail.append(
                    {
                        "id": folder.id,
                        "name": folder.name,
                        "parent_id": folder.parent_id,
                        "path": _build_folder_path(folder.id, folder_map),
                    },
                )

    return {
        "changed_files_detail": changed_files_detail,
        "changed_folders_detail": changed_folders_detail,
    }


def _to_payload_text(payload: JsonValue) -> str | None:
    """Serialise the optional payload column; plain strings pass through."""
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def log_event(params: LogEventParams) -> bool:
    """Append one change event; a write failure returns False instead of raising."""
    return (
        log_events_batch(
            params.workspace_id,
            [
                {
                    "entity_type": params.entity_type,
                    "entity_id": params.entity_id,
                    "action": params.action,
                    "old_parent_id": params.old_parent_id,
                    "new_parent_id": params.new_parent_id,
                    "old_name": params.old_name,
                    "new_name": params.new_name,
                    "payload": params.payload,
                },
            ],
        )
        > 0
    )


def log_events_batch(workspace_id: int, events: Sequence[FileChangeEventSpec]) -> int:
    """Append change events; uses its own session so callers keep their transaction."""
    if not events:
        return 0

    rows: list[FileChangeEvent] = []
    for event in events:
        entity_id = event.get("entity_id")
        if entity_id is None:
            continue
        rows.append(
            FileChangeEvent(
                workspace_id=workspace_id,
                entity_type=str(event.get("entity_type") or "unknown"),
                entity_id=int(entity_id),
                action=str(event.get("action") or "update_meta"),
                old_parent_id=event.get("old_parent_id"),
                new_parent_id=event.get("new_parent_id"),
                old_name=event.get("old_name"),
                new_name=event.get("new_name"),
                payload=_to_payload_text(event.get("payload")),
            ),
        )

    if not rows:
        return 0

    session = SessionLocal()
    try:
        session.add_all(rows)
        session.commit()
        return len(rows)
    except Exception as exc:
        session.rollback()
        logger.warning(
            "Failed to write change events for workspace {} (error_type={})",
            workspace_id,
            type(exc).__name__,
        )
        return 0
    finally:
        session.close()


def get_latest_event_id(session: Session, workspace_id: int) -> int:
    """Return the newest event id of a workspace, or 0 when it has none."""
    row = (
        session.query(FileChangeEvent)
        .filter_by(workspace_id=workspace_id)
        .order_by(FileChangeEvent.id.desc())
        .first()
    )
    return int(row.id) if row else 0


def get_checkpoint_event_id(session: Session, workspace_id: int) -> int:
    """Return the checkpointed event id of a workspace, or 0 when absent."""
    checkpoint = (
        session.query(OrganizeCheckpoint).filter_by(workspace_id=workspace_id).first()
    )
    if not checkpoint:
        return 0
    return int(checkpoint.last_event_id or 0)


def update_checkpoint(
    session: Session,
    workspace_id: int,
    event_id: int,
    *,
    mark_full_scan: bool = False,
) -> None:
    """Advance the organize checkpoint; `last_event_id` never moves backwards."""
    target_event_id = max(0, int(event_id or 0))
    now = utc_now()
    statement = postgres_insert(OrganizeCheckpoint).values(
        workspace_id=workspace_id,
        last_event_id=target_event_id,
        last_full_scan_at=now if mark_full_scan else None,
        updated_at=now,
    )
    values: dict[str, object] = {
        "last_event_id": case(
            (
                OrganizeCheckpoint.last_event_id < statement.excluded.last_event_id,
                statement.excluded.last_event_id,
            ),
            else_=OrganizeCheckpoint.last_event_id,
        ),
        "updated_at": now,
    }
    if mark_full_scan:
        values["last_full_scan_at"] = now
    statement = statement.on_conflict_do_update(
        index_elements=[OrganizeCheckpoint.workspace_id], set_=values,
    )
    try:
        session.execute(statement)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error(
            "Failed to update organize checkpoint for workspace {} (error_type={})",
            workspace_id,
            type(exc).__name__,
        )
        raise


def load_incremental_context(
    session: Session,
    workspace_id: int,
    max_events: int = DEFAULT_MAX_INCREMENTAL_EVENTS,
) -> IncrementalContext:
    """Load the change summary since the checkpoint.

    `overflow` tells the caller to fall back to a full scan.
    """
    max_events = max(1, int(max_events or DEFAULT_MAX_INCREMENTAL_EVENTS))

    checkpoint = (
        session.query(OrganizeCheckpoint).filter_by(workspace_id=workspace_id).first()
    )
    has_checkpoint = checkpoint is not None
    checkpoint_event_id = int(checkpoint.last_event_id or 0) if checkpoint else 0
    target_event_id = get_latest_event_id(session, workspace_id)

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
            "changed_files_detail": [],
            "changed_folders_detail": [],
        }

    query = (
        session.query(FileChangeEvent)
        .filter(FileChangeEvent.workspace_id == workspace_id)
        .filter(FileChangeEvent.id > checkpoint_event_id)
        .filter(FileChangeEvent.id <= target_event_id)
        .order_by(FileChangeEvent.id.asc())
    )
    total_events = query.count()
    events = query.limit(max_events).all()
    overflow = total_events > max_events

    summary: EventSummary = summarize_events(
        events,
        total_count=total_events,
        from_event_id=checkpoint_event_id,
        to_event_id=target_event_id,
    )

    details = _resolve_changed_details(
        session,
        workspace_id,
        summary["changed_file_ids"],
        summary["changed_folder_ids"],
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
        "changed_files_detail": details["changed_files_detail"],
        "changed_folders_detail": details["changed_folders_detail"],
    }
