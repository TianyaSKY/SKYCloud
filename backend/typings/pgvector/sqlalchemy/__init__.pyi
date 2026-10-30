"""Type declarations for pgvector's SQLAlchemy column types."""

from collections.abc import Sequence

from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import UserDefinedType


class _VectorComparator(UserDefinedType.Comparator):
    def l2_distance(
        self, other: Sequence[float] | ColumnElement[Sequence[float]]
    ) -> ColumnElement[float]: ...
    def max_inner_product(
        self, other: Sequence[float] | ColumnElement[Sequence[float]]
    ) -> ColumnElement[float]: ...
    def cosine_distance(
        self, other: Sequence[float] | ColumnElement[Sequence[float]]
    ) -> ColumnElement[float]: ...
    def l1_distance(
        self, other: Sequence[float] | ColumnElement[Sequence[float]]
    ) -> ColumnElement[float]: ...


class VECTOR(UserDefinedType[Sequence[float]]):
    class comparator_factory(_VectorComparator): ...
    dim: int | None
    def __init__(self, dim: int | None = None) -> None: ...


Vector = VECTOR
