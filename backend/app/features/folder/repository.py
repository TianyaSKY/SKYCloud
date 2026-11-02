"""文件夹最新状态读取与事务作用域目录树变更串行化."""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.folder import Folder

TREE_LOCK_NAMESPACE = 1179601988  # 与整理器自身的执行租约相互独立。


def lock_tree(session: Session, workspace_id: int) -> None:
    """串行化单个工作空间的目录树变更, 直至事务结束."""
    # 生产环境使用 PostgreSQL。事务会在 commit、rollback
    # 或连接断开 (含进程崩溃) 时自动释放该锁。
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :workspace)"),
            {"namespace": TREE_LOCK_NAMESPACE, "workspace": workspace_id},
        )


def workspace_id_for_folder(session: Session, folder_id: int) -> int | None:
    """返回文件夹归属的工作空间 ID, 无需加载完整实体行."""
    return session.scalar(
        select(Folder.workspace_id)
        .where(Folder.id == folder_id)
        .execution_options(autoflush=False),
    )


def get_folder(session: Session, folder_id: int) -> Folder | None:
    """读取文件夹行, 刷新当前会话中 identity-map 缓存的实例."""
    return session.get(Folder, folder_id, populate_existing=True)


def lock_folder(session: Session, folder_id: int) -> Folder | None:
    """在当前事务中加行级排他锁加载文件夹记录."""
    return session.scalar(
        select(Folder)
        .where(Folder.id == folder_id)
        .with_for_update()
        .execution_options(populate_existing=True, autoflush=False),
    )


def root_id(session: Session, workspace_id: int) -> int | None:
    """返回工作空间的根文件夹 ID."""
    # 确定性排序选择亦可兜底兼容历史多余根节点的恢复。
    return session.scalar(
        select(Folder.id)
        .where(Folder.workspace_id == workspace_id, Folder.parent_id.is_(None))
        .order_by(Folder.id)
        .limit(1),
    )
