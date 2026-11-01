"""Initialize the final schema in a clean PostgreSQL database."""

from pathlib import Path

from alembic import op
from sqlalchemy import inspect

revision = "0001_rebuild"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    existing_tables = set(inspect(connection).get_table_names(schema="public")) - {"alembic_version"}
    if existing_tables:
        tables = ", ".join(sorted(existing_tables))
        raise RuntimeError(
            "Unversioned database contains existing tables; refusing to rebuild it: "
            f"{tables}"
        )

    snapshot = Path(__file__).resolve().parents[1] / "0001_schema.sql"
    connection.exec_driver_sql(snapshot.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError(
        "Baseline downgrade would delete user data; restore a verified database backup instead"
    )
