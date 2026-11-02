"""只读启动校验门禁. 数据库架构变更属于 entry.migrate."""

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

EXPECTED_REVISION = "0001_rebuild"


def _revisions(connection: Connection) -> set[str]:
    return set(
        connection.execute(text("SELECT version_num FROM alembic_version")).scalars(),
    )


def check_schema(database_engine: Engine | Connection) -> None:
    """在不改变数据库的前提下, 拒绝不可用或版本不匹配的数据库架构."""
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
