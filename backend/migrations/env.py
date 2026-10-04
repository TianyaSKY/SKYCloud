"""Transactional migrations with a caller-owned PostgreSQL connection."""

from alembic import context
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def run(connection):
    if connection.dialect.name != "postgresql":
        raise RuntimeError("Deployment migrations require PostgreSQL")
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required for migrations")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            connection.execute(text("SELECT pg_advisory_xact_lock(731948211)"))
            run(connection)
    finally:
        engine.dispose()
