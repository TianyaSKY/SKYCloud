"""Persist indexing deliveries atomically with files."""
from alembic import op

revision = "0007_file_task_outbox"
down_revision = "0006_user_role_type"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE file_task_outbox (
        id SERIAL PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        delivered_at TIMESTAMPTZ,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error_type VARCHAR(96)
    )""")
    op.execute("CREATE INDEX ix_file_task_outbox_pending ON file_task_outbox(id) WHERE delivered_at IS NULL")
    # Recover previously interrupted/lost indexing work without automatically
    # retrying business failures. Duplicate legacy queue messages are possible.
    op.execute("""INSERT INTO file_task_outbox(file_id)
        SELECT id FROM files WHERE status IN ('pending', 'processing')""")


def downgrade():
    raise RuntimeError("Restore a verified backup instead of discarding pending indexing deliveries")
