"""Persist remote abort intents independently of deleted assistant/account rows."""
from alembic import op

revision = '0017_assistant_abort'
down_revision = '0016_generation_retirement'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE assistant_runs ADD COLUMN runtime_lease_token VARCHAR(32)')
    op.execute('ALTER TABLE assistant_runs ADD COLUMN runtime_lease_runtime_id INTEGER')
    op.execute('ALTER TABLE assistant_runs ADD COLUMN runtime_lease_container_id VARCHAR(64)')
    op.execute("""CREATE TABLE assistant_runtime_aborts (
        run_id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        runtime_id INTEGER NOT NULL, container_id VARCHAR(64) NOT NULL, generation VARCHAR(32),
        session_id VARCHAR(128), lease_token VARCHAR(32),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0, last_error_type VARCHAR(96)
    )""")
    op.execute('CREATE INDEX ix_assistant_runtime_aborts_pending ON assistant_runtime_aborts(workspace_id,user_id) WHERE completed_at IS NULL')


def downgrade():
    raise RuntimeError('Restore a verified backup instead of discarding pending remote session aborts')
