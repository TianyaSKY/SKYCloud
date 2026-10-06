"""Durable organizer admission, delivery and fenced completion."""
from alembic import op

revision = "0008_organize_tasks"
down_revision = "0007_file_task_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE organize_tasks (
        id VARCHAR(64) PRIMARY KEY,
        workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        state VARCHAR(20) NOT NULL DEFAULT 'queued',
        execution_token VARCHAR(64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        delivered_at TIMESTAMPTZ,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error_type VARCHAR(96),
        finished_at TIMESTAMPTZ
    )""")
    op.execute("""CREATE UNIQUE INDEX uq_organize_active_workspace
        ON organize_tasks(workspace_id) WHERE state IN ('queued','running')""")


def downgrade():
    raise RuntimeError("Restore a verified backup instead of discarding organizer task identity")


