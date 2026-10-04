"""Adopt unversioned deployments or initialize an empty PostgreSQL database.

This revision uses an immutable SQL snapshot, never live ORM metadata.
"""

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Old deployments may already have these tables but lack newer columns.
    repairs = {
        "files": ["content_hash VARCHAR(64)"],
        "mcp_tokens": ["token_value TEXT"],
        "workspaces": ["container_id VARCHAR(64)", "status VARCHAR(20) NOT NULL DEFAULT 'stopped'", "error_message TEXT"],
        "assistant_runs": ["permission_response VARCHAR(16)"],
    }
    for table, columns in repairs.items():
        for column in columns:
            op.execute(f"""DO $$ BEGIN
                IF to_regclass('{table}') IS NOT NULL THEN
                    ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column};
                END IF;
            END $$""")
    sql = (Path(__file__).resolve().parents[1] / "0001_schema.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)


def downgrade():
    raise RuntimeError("Baseline downgrade would delete user data; restore a verified database backup instead")
