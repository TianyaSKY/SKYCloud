"""Retire completed generations after their last SQL container reference detaches."""
from alembic import op

revision = '0016_generation_retirement'
down_revision = '0015_runtime_operations'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE INDEX ix_runtime_operations_candidate ON runtime_operations(candidate_container_id)')
    # A completed cleanup marker from an older application does not prove that
    # the Controller generation fence was committed. Reuse durable cancellation.
    op.execute("""UPDATE runtime_operations AS operation
        SET state='cancelled', finished_at=NULL, next_attempt_at=clock_timestamp(), last_error_type=NULL
        WHERE state='complete' AND candidate_container_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM opencode_runtimes WHERE container_id=operation.candidate_container_id)
        AND NOT EXISTS (SELECT 1 FROM workspaces WHERE container_id=operation.candidate_container_id)""")
    op.execute("""CREATE FUNCTION retire_detached_completed_generation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.container_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM opencode_runtimes WHERE container_id=OLD.container_id
            ) AND NOT EXISTS (SELECT 1 FROM workspaces WHERE container_id=OLD.container_id) THEN
                UPDATE runtime_operations SET state='cancelled', finished_at=NULL,
                    next_attempt_at=clock_timestamp(), last_error_type=NULL
                WHERE state='complete' AND candidate_container_id=OLD.container_id;
            END IF;
            RETURN OLD;
        END $$""")
    for table in ('opencode_runtimes', 'workspaces'):
        op.execute(f"""CREATE TRIGGER retire_{table}_generation_delete AFTER DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION retire_detached_completed_generation()""")
        op.execute(f"""CREATE TRIGGER retire_{table}_generation_detach AFTER UPDATE OF container_id ON {table}
            FOR EACH ROW WHEN (OLD.container_id IS DISTINCT FROM NEW.container_id)
            EXECUTE FUNCTION retire_detached_completed_generation()""")


def downgrade():
    raise RuntimeError('Restore a verified backup instead of discarding pending container cleanup or generation retirement intents')
