"""Tests for schema version verification at startup."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from app.infra.schema import EXPECTED_REVISION, check_schema


def _versioned_engine(revisions: list[str]) -> Engine:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
        for revision in revisions:
            conn.execute(
                text("INSERT INTO alembic_version VALUES (:revision)"),
                {"revision": revision},
            )
    return engine


@pytest.mark.parametrize(
    "revisions",
    [[], ["older"], ["future"], [EXPECTED_REVISION, "branch"]],
)
def test_startup_rejects_schema_mismatch(revisions: list[str]) -> None:
    engine = _versioned_engine(revisions)
    try:
        with pytest.raises(RuntimeError, match="version differs"):
            check_schema(engine)
        with engine.connect() as conn:
            stored = set(conn.scalars(text("SELECT version_num FROM alembic_version")))
            assert stored == set(revisions)
    finally:
        engine.dispose()


def test_startup_rejects_unversioned_database() -> None:
    engine = create_engine("sqlite:///:memory:")
    try:
        with pytest.raises(RuntimeError, match="unversioned"):
            check_schema(engine)
        with engine.connect() as conn:
            assert inspect(conn).get_table_names() == []
    finally:
        engine.dispose()
