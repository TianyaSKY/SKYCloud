"""Run explicit versioned database upgrades before starting application processes."""

import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from app.infra.schema import EXPECTED_REVISION
from dotenv import load_dotenv
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.engine import Engine

_DEFAULT_REVISION = "head"


def migration_config(connection: Connection) -> Config:
    """Build an Alembic config bound to the already opened connection."""
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _validate_database_state(connection: Connection) -> None:
    """Allow only a pristine database or this exact initialized baseline."""
    version_table = connection.scalar(
        text("SELECT to_regclass('public.alembic_version')"),
    )
    if version_table is not None:
        revisions = set(
            connection.execute(
                text("SELECT version_num FROM alembic_version"),
            ).scalars(),
        )
        if revisions == {EXPECTED_REVISION}:
            return
        message = (
            "Existing or unversioned Alembic history is unsupported; "
            "initialize a new empty database"
        )
        raise RuntimeError(message)

    existing_tables = inspect(connection).get_table_names(schema="public")
    if existing_tables:
        tables = ", ".join(sorted(existing_tables))
        message = (
            "Unversioned database contains existing tables; refusing to initialize it: "
            f"{tables}"
        )
        raise RuntimeError(message)


def upgrade(database_engine: Engine, revision: str = _DEFAULT_REVISION) -> None:
    """Apply the baseline under one advisory-locked transaction."""
    with database_engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            message = "Deployment migrations require PostgreSQL"
            raise RuntimeError(message)
        connection.execute(text("SET LOCAL lock_timeout = '30s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(731948211)"))
        _validate_database_state(connection)
        command.upgrade(migration_config(connection), revision)

def main() -> None:
    """Load configuration and apply migrations to the configured database."""
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        message = "DATABASE_URL is required for migrations"
        raise RuntimeError(message)
    database_engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        upgrade(database_engine)
    finally:
        database_engine.dispose()
    sys.stdout.write("Database migrations completed\n")


if __name__ == "__main__":
    main()
