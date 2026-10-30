"""Read-only startup gate. Schema changes belong to entry.migrate."""

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

EXPECTED_REVISION = "0019_sql_cache_revisions"


def _revisions(connection: Connection) -> set[str]:
    return set(
        connection.execute(text("SELECT version_num FROM alembic_version")).scalars(),
    )


def check_schema(database_engine: Engine | Connection) -> None:
    """Reject unavailable or mismatched schemas without changing the database."""
    try:
        if isinstance(database_engine, Connection):
            revisions = _revisions(database_engine)
        else:
            with database_engine.connect() as connection:
                revisions = _revisions(connection)
    except SQLAlchemyError as exc:
        message = (
            "Database schema is unavailable or unversioned; "
            "run python -m entry.migrate"
        )
        raise RuntimeError(message) from exc
    if revisions != {EXPECTED_REVISION}:
        message = (
            "Database schema version differs from this application; "
            "run python -m entry.migrate before startup"
        )
        raise RuntimeError(message)
