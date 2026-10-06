"""Read-only startup gate. Schema changes belong to entry.migrate."""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

EXPECTED_REVISION = "0014_container_deletions"


def check_schema(database_engine):
    try:
        with database_engine.connect() as connection:
            revisions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    except SQLAlchemyError as exc:
        raise RuntimeError("Database schema is unavailable or unversioned; run python -m entry.migrate") from exc
    if revisions != {EXPECTED_REVISION}:
        raise RuntimeError("Database schema version differs from this application; run python -m entry.migrate before startup")
