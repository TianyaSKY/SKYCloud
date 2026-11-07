"""Initialize an empty PostgreSQL database before starting application processes."""

import os
import sys
from pathlib import Path

from app import models
from app.infra.extensions import Base
from app.infra.schema import EXPECTED_REVISION
from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    Connection,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    create_engine,
    insert,
    inspect,
    text,
)
from sqlalchemy.engine import Engine

# Keep the original marker so previously initialized databases remain usable
# without an in-place migration or any changes to their contents.
_VERSION_TABLE = Table(
    "alembic_version",
    MetaData(),
    Column("version_num", String(32), nullable=False),
    PrimaryKeyConstraint("version_num", name="alembic_version_pkc"),
    schema="public",
)
_POSTGRES_SCHEMA = (
    Path(__file__).resolve().parents[1] / "app" / "infra" / "postgres_schema.sql"
)


def _is_initialized(connection: Connection) -> bool:
    """Accept only a matching existing baseline or a completely empty database."""
    version_table = connection.scalar(
        text("SELECT to_regclass('public.alembic_version')"),
    )
    if version_table is not None:
        revisions = set(
            connection.execute(
                text("SELECT version_num FROM public.alembic_version"),
            ).scalars(),
        )
        if revisions == {EXPECTED_REVISION}:
            return True
        message = (
            "Database schema version is unsupported; "
            "use a compatible database or initialize a new empty database"
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
    return False


def initialize_database(database_engine: Engine) -> None:
    """Initialize an empty database atomically; preserve an existing baseline."""
    if database_engine.dialect.name != "postgresql":
        message = "Database initialization requires PostgreSQL"
        raise RuntimeError(message)

    with database_engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '30s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(731948211)"))
        connection.execute(text("SET LOCAL search_path = public"))
        if _is_initialized(connection):
            return

        connection.exec_driver_sql(
            "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public",
        )
        Base.metadata.create_all(bind=connection, checkfirst=False)
        connection.exec_driver_sql(_POSTGRES_SCHEMA.read_text(encoding="utf-8"))
        connection.execute(
            insert(models.SysDict).values(
                key="site_name",
                value="SKYCloud",
                des="站点名称（显示在网页标题与 Logo 旁）",  # noqa: RUF001
            ),
        )
        _VERSION_TABLE.create(connection)
        connection.execute(_VERSION_TABLE.insert().values(version_num=EXPECTED_REVISION))


def main() -> None:
    """Load the database URL and initialize the schema if the database is empty."""
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        message = "DATABASE_URL is required for database initialization"
        raise RuntimeError(message)
    database_engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        initialize_database(database_engine)
    finally:
        database_engine.dispose()
    sys.stdout.write("Database initialization completed\n")


if __name__ == "__main__":
    main()
