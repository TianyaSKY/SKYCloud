"""分享发布所需的实时文件作用域查询与行锁定."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.file import File


def file_workspace_id(session: Session, file_id: int) -> int | None:
    """读取文件的实时工作区作用域且不触发 flush."""
    return session.scalar(
        select(File.workspace_id)
        .where(File.id == file_id)
        .execution_options(autoflush=False),
    )


def lock_scoped_file(session: Session, file_id: int, workspace_id: int) -> File | None:
    """锁定单条文件记录, 验证其仍归属于指定工作区."""
    return session.scalar(
        select(File)
        .where(File.id == file_id, File.workspace_id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True),
    )
