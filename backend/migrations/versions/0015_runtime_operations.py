"""Persist replacement identities and transactional lifecycle cancellation."""
from alembic import op

revision = '0015_runtime_operations'
down_revision = '0014_container_deletions'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE runtime_operations (
        generation VARCHAR(32) PRIMARY KEY, workspace_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, runtime_id INTEGER NOT NULL,
        previous_container_id VARCHAR(64), candidate_container_id VARCHAR(64),
        state VARCHAR(20) NOT NULL DEFAULT 'preparing',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0, last_error_type VARCHAR(96),
        CONSTRAINT ck_runtime_operation_state CHECK (state IN ('preparing','switched','complete','cancelled'))
    )""")
    op.execute("CREATE UNIQUE INDEX uq_runtime_operation_active ON runtime_operations(runtime_id) WHERE state IN ('preparing','switched')")
    op.execute('CREATE INDEX ix_runtime_operations_pending ON runtime_operations(next_attempt_at,generation) WHERE finished_at IS NULL')
    op.execute("""CREATE FUNCTION cancel_deleted_runtime_operation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE runtime_operations SET state='cancelled', next_attempt_at=now()
            WHERE runtime_id=OLD.id AND state IN ('preparing','switched');
            RETURN OLD;
        END $$""")
    op.execute("""CREATE TRIGGER cancel_deleted_runtime_operation AFTER DELETE ON opencode_runtimes
        FOR EACH ROW EXECUTE FUNCTION cancel_deleted_runtime_operation()""")
    op.execute("""CREATE FUNCTION cancel_changed_runtime_operation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE runtime_operations SET state='cancelled', next_attempt_at=now()
            WHERE runtime_id=OLD.id AND state IN ('preparing','switched') AND (
                (OLD.container_id IS DISTINCT FROM NEW.container_id AND candidate_container_id IS DISTINCT FROM NEW.container_id)
                OR (OLD.status IS DISTINCT FROM NEW.status AND NEW.status='stopped'));
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER cancel_changed_runtime_operation AFTER UPDATE OF container_id,status ON opencode_runtimes
        FOR EACH ROW EXECUTE FUNCTION cancel_changed_runtime_operation()""")
    op.execute("""CREATE FUNCTION cancel_member_runtime_operation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE runtime_operations SET state='cancelled', next_attempt_at=now()
            WHERE workspace_id=OLD.workspace_id AND user_id=OLD.user_id AND state IN ('preparing','switched');
            RETURN OLD;
        END $$""")
    op.execute("""CREATE TRIGGER cancel_removed_member_runtime_operation AFTER DELETE ON workspace_members
        FOR EACH ROW EXECUTE FUNCTION cancel_member_runtime_operation()""")
    op.execute("""CREATE TRIGGER cancel_demoted_member_runtime_operation AFTER UPDATE OF role ON workspace_members
        FOR EACH ROW WHEN (NEW.role NOT IN ('editor','admin')) EXECUTE FUNCTION cancel_member_runtime_operation()""")


def downgrade():
    raise RuntimeError('Restore a verified backup instead of discarding pending container cleanup or runtime operations')
