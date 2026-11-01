"""Shared structural contracts for explicitly closable async iterators."""

from collections.abc import Awaitable
from typing import Protocol, Self, TypeVar

T_co = TypeVar("T_co", covariant=True)


class AsyncClosableIterator(Protocol[T_co]):
    """An owned async iterator that can release its underlying read."""

    def __aiter__(self) -> Self:
        """Return the iterator itself, as async iterators do."""
        ...

    def __anext__(self) -> Awaitable[T_co]:
        """Return the next item from the owned read."""
        ...

    async def aclose(self) -> None:
        """Close the owned read."""
        ...
