"""与文件夹删除共享的文件删除及搜索缓存失效逻辑.

文件夹级联删除会导入这些辅助函数; 本模块在依赖图上位于文件服务下方,
且绝不循环导入文件夹服务。
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
    """清除工作空间下缓存的模糊搜索与语义搜索结果."""
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
    """删除单条文件记录并为 Worker 暂存其对象清理任务.

    当递归批量删除持有主事务并自行记录变更流水时,
    ``commit`` 与 ``log_event`` 参数传入 False。
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
