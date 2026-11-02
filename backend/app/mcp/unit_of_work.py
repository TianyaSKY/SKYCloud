"""Worker 拥有的 MCP 会话清理; 调用方保留所有提交/回滚决断权."""

from collections.abc import Generator
from contextlib import contextmanager

from loguru import logger
from sqlalchemy.orm import Session

from app.infra.extensions import SessionFactory


@contextmanager
def worker_session(session_factory: SessionFactory) -> Generator[Session, None, None]:
    """提供 worker 拥有的会话, 并在退出时始终将其销毁."""
    session = session_factory()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception as exc:
            # 会话/连接池/驱动层清理可能引发任意异常。保留
            # 调用方的结果或原始异常, 随后丢弃会话句柄。
            logger.error(
                "MCP session close failed: error_type={}", type(exc).__name__,
            )
            # 丢弃该连接而非将清理失败的连接归还到
            # 连接池中。失效操作还会回滚所有未提交的工作。
            try:
                session.invalidate()
            except Exception as cleanup_exc:
                # 最终的驱动层失效操作失败同样绝不能
                # 覆盖业务处理结果或泄露数据库连接细节。
                logger.error(
                    "MCP session invalidation failed: error_type={}",
                    type(cleanup_exc).__name__,
                )
