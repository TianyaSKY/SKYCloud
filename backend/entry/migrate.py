"""Run explicit versioned database upgrades before starting application processes."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def migration_config(connection):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def upgrade(database_engine, revision="head"):
    with database_engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise RuntimeError("Deployment migrations require PostgreSQL")
        connection.execute(text("SET LOCAL lock_timeout = '30s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(731948211)"))
        command.upgrade(migration_config(connection), revision)


def main():
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required for migrations")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        upgrade(engine)
    finally:
        engine.dispose()
    print("Database migrations completed")


if __name__ == "__main__":
    main()
