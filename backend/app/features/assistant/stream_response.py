"""即使 ASGI 在进入迭代器前失败也关闭准备好的运行."""

import asyncio
from collections.abc import AsyncIterator, Callable, Generator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, TypeVar, cast

import anyio
from loguru import logger
from runtime_common.http_stream import close_reader, drain_cleanup
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.features.assistant import repository
from app.features.assistant.service import cleanup_stream_session
from app.features.assistant.stream_persistence import StreamWorker
from app.infra.extensions import SessionFactory

if TYPE_CHECKING:
    from runtime_common.async_stream import AsyncClosableIterator

T = TypeVar("T")
RunScope = tuple[int, int, int, int]


@contextmanager
def preparation_session(
    session_factory: SessionFactory,
) -> Generator[Session, None, None]:
    """产出准备 Session 并始终在所属 worker 上关闭."""
    session = session_factory()
    try:
        yield session
    finally:
        cleanup_stream_session(session, "close")


async def prepare_response_run(
    prepare: Callable[[Callable[[RunScope], None]], T],
    session_factory: SessionFactory,
) -> T:
    """在补偿中断的 HTTP 请求前排干 SQL 准备操作."""
    created_scope: RunScope | None = None

    def capture(scope: RunScope) -> None:
        nonlocal created_scope
        created_scope = scope

    async with StreamWorker() as worker:
        try:
            return await worker.call(prepare, capture)
        except BaseException:
            if created_scope is not None:
                with anyio.CancelScope(shield=True):
                    await worker.call(
                        cleanup_unclaimed_run,
                        session_factory,
                        created_scope,
                    )
            raise


def cleanup_unclaimed_run(
    session_factory: SessionFactory,
    scope: RunScope,
) -> None:
    """补偿 HTTP 请求从未开始流式传输的已准备运行."""
    # 内部补偿使用确切准备的标识, 即使移除成员身份后仍可运行.
    # 此逻辑并非面向用户的取消 API.
    session = None
    try:
        session = session_factory()
        (
            workspace_id,
            user_id,
            conversation_id,
            run_id,
        ) = scope
        repository.cancel_unclaimed_run(
            session,
            workspace_id,
            user_id,
            conversation_id,
            run_id,
        )
        session.commit()
    except Exception as exc:
        logger.error(
            "Assistant unclaimed run cleanup failed (error_type={})",
            type(exc).__name__,
        )
        if session is not None:
            cleanup_stream_session(session, "rollback")
    finally:
        if session is not None:
            cleanup_stream_session(session, "close")


class PreparedStreamResponse(StreamingResponse):
    """释放未认领已准备运行的流式响应."""

    def __init__(
        self,
        content: AsyncIterator[str | bytes],
        *,
        session_factory: SessionFactory,
        run_scope: RunScope,
        media_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """将响应绑定至其必须补偿的标识."""
        super().__init__(content, media_type=media_type, headers=headers)
        self.session_factory = session_factory
        self.run_scope = run_scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """流式传输内容并在任何结果下释放已准备的运行."""
        failed = False
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            failed = True
            raise
        finally:
            async def release() -> None:
                iterator = cast(
                    "AsyncClosableIterator[str | bytes]",
                    self.body_iterator,
                )
                await close_reader(None, iterator)
                async with StreamWorker() as worker:
                    await worker.call(
                        cleanup_unclaimed_run,
                        self.session_factory,
                        self.run_scope,
                    )

            cancelled = await drain_cleanup(release())
            if cancelled and not failed:
                raise asyncio.CancelledError
