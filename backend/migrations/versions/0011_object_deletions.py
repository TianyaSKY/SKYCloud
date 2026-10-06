"""Commit file object cleanup alongside metadata deletion."""
from alembic import op

revision = "0011_object_deletions"
down_revision = "0010_redact_mcp_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE file_object_deletions (
        id SERIAL PRIMARY KEY, object_name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
        last_error_type VARCHAR(96)
    )""")
    op.execute("CREATE INDEX ix_file_object_deletions_pending ON file_object_deletions(id) WHERE completed_at IS NULL")
    op.execute("CREATE INDEX ix_files_object_reference ON files(file_path)")


def downgrade():
    raise RuntimeError("Restore a verified backup instead of discarding pending object cleanup")
