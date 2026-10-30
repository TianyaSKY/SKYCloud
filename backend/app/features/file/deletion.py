"""File deletion and search-cache invalidation shared with folder deletion.

The folder cascade imports these helpers, so this module stays below the file
service in the import graph and never imports the folder service.
"""

from sqlalchemy.orm import Session

from app.exceptions import ResourceNotFoundError
from app.features.file import repository
from app.features.file.search_service import (
    SEARCH_CACHE_PREFIX,
    SEARCH_GENERATION_PREFIX,
)
from app.features.folder import change_log as change_log_service
from app.infra.cache import evict_cache_pattern, invalidate_cache_generation
from app.infra.file_object_deletion import stage_object_deletion_fallback


def clear_search_cache(workspace_id: int | None) -> None:
    """Drop the fuzzy and semantic search results cached for the workspace."""
    if workspace_id is not None:
        invalidate_cache_generation(f"{SEARCH_GENERATION_PREFIX}:{workspace_id}")
        evict_cache_pattern(f"{SEARCH_CACHE_PREFIX}:{workspace_id}")


def delete_file(
    session: Session,
    file_id: int,
    *,
    commit: bool = True,
    log_event: bool = True,
) -> None:
    """Delete one file row and stage its object cleanup for the worker.

    ``commit``/``log_event`` are false when a recursive batch delete owns the
    transaction and writes the change log itself.
    """
    file_obj = repository.find_file(session, file_id)
    if not file_obj:
        message = "File not found"
        raise ResourceNotFoundError(message)

    workspace_id = file_obj.workspace_id
    old_parent_id = file_obj.parent_id
    old_name = file_obj.name
    entity_id = file_obj.id
    object_name = file_obj.file_path

    stage_object_deletion_fallback(session, object_name)
    repository.remove_file(session, file_obj)
    if commit:
        session.commit()
        if log_event:
            change_log_service.log_event(
                change_log_service.LogEventParams(
                    workspace_id=workspace_id,
                    entity_type="file",
                    entity_id=entity_id,
                    action="delete",
                    old_parent_id=old_parent_id,
                    new_parent_id=None,
                    old_name=old_name,
                    new_name=None,
                ),
            )
        clear_search_cache(workspace_id)
