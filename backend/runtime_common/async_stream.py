"""显式可关闭异步迭代器的共享结构化契约."""

from collections.abc import Awaitable
from typing import Protocol, Self, TypeVar

T_co = TypeVar("T_co", covariant=True)


class AsyncClosableIterator(Protocol[T_co]):
    """具备所有权且能主动释放底层读取资源的异步迭代器."""

    def __aiter__(self) -> Self:
        """返回迭代器自身."""
        ...

    def __anext__(self) -> Awaitable[T_co]:
        """获取所属读取资源的下一项."""
        ...

    async def aclose(self) -> None:
        """关闭持有的读取资源."""
        ...
