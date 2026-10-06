"""Complete account cascades while preserving nullable invitation attribution."""
from alembic import op
from sqlalchemy import text

revision = "0013_account_delete_fks"
down_revision = "0012_object_delete_trigger"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for table, column, action in (
        ("workspaces", "owner_id", "CASCADE"),
        ("mcp_tokens", "user_id", "CASCADE"),
        ("shares", "user_id", "CASCADE"),
        ("workspace_members", "invited_by", "SET NULL"),
    ):
        names = connection.execute(text("""SELECT c.conname FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attname=:column
            WHERE c.conrelid=CAST(:table AS regclass) AND c.confrelid='users'::regclass
            AND c.contype='f' AND c.conkey=ARRAY[a.attnum]"""), {"table": table, "column": column}).scalars().all()
        for name in names:
            quoted = name.replace('"', '""')
            op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{quoted}"')
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_{column}_users "
                   f"FOREIGN KEY ({column}) REFERENCES users(id) ON DELETE {action}")


def downgrade():
    raise RuntimeError("Restore a verified backup instead of bypassing account deletion cascades")
