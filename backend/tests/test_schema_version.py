"""Tests for Alembic schema version verification at startup."""

from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, text

from app.infra.schema import EXPECTED_REVISION, check_schema


def test_startup_revision_matches_migration_head() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == [EXPECTED_REVISION]


@pytest.mark.parametrize(
    "revisions",
    [[], ["older"], ["future"], [EXPECTED_REVISION, "branch"]],
)
def test_startup_rejects_schema_mismatch(revisions: list[str]) -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
        for revision in revisions:
            conn.execute(
                text("INSERT INTO alembic_version VALUES (:revision)"),
                {"revision": revision},
            )
    with pytest.raises(RuntimeError, match="version differs"):
        check_schema(engine)
    engine.dispose()


def test_startup_rejects_unversioned_database() -> None:
    engine = create_engine("sqlite:///:memory:")
    with pytest.raises(RuntimeError, match="unversioned"):
        check_schema(engine)
    engine.dispose()
