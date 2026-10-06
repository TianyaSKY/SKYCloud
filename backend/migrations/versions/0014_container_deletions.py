"""Capture deleted/detached runtime and legacy workspace container identities."""
from alembic import op

revision = '0014_container_deletions'
down_revision = '0013_account_delete_fks'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE runtime_container_deletions (
        id SERIAL PRIMARY KEY, container_id VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
        last_error_type VARCHAR(96)
    )""")
    op.execute('CREATE INDEX ix_runtime_container_deletions_pending ON runtime_container_deletions(id) WHERE completed_at IS NULL')
    op.execute("""CREATE FUNCTION stage_detached_runtime_container() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO runtime_container_deletions(container_id) VALUES (OLD.container_id);
            RETURN OLD;
        END $$""")
    for table in ('opencode_runtimes', 'workspaces'):
        op.execute(f"""CREATE TRIGGER stage_{table}_container_delete AFTER DELETE ON {table}
            FOR EACH ROW WHEN (OLD.container_id IS NOT NULL)
            EXECUTE FUNCTION stage_detached_runtime_container()""")
        op.execute(f"""CREATE TRIGGER stage_{table}_container_detach AFTER UPDATE OF container_id ON {table}
            FOR EACH ROW WHEN (OLD.container_id IS NOT NULL AND OLD.container_id IS DISTINCT FROM NEW.container_id)
            EXECUTE FUNCTION stage_detached_runtime_container()""")


def downgrade():
    raise RuntimeError('Restore a verified backup instead of discarding pending container cleanup')
