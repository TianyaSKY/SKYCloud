"""Preserve historical instants while replacing naive timestamps with timestamptz."""
from alembic import op
from sqlalchemy import text

revision = "0009_utc_timestamps"
down_revision = "0008_organize_tasks"
branch_labels = None
depends_on = None

BEIJING_COLUMNS = {
    "users": ["created_at", "last_active_at"],
    "sys_dict": ["created_at"], "token_usage_logs": ["created_at"],
    "inbox": ["created_at"], "files": ["created_at"], "folder": ["created_at"],
    "workspaces": ["created_at", "updated_at"], "workspace_members": ["joined_at"],
    "shares": ["created_at", "expires_at"], "file_chunks": ["created_at"],
    "file_change_events": ["created_at"], "mcp_audit_logs": ["created_at"],
    "mcp_tokens": ["created_at", "expires_at", "last_used_at", "revoked_at"],
    "mcp_runtime_tokens": ["created_at", "expires_at", "last_used_at", "revoked_at"],
    "opencode_runtimes": ["created_at", "updated_at", "last_started_at", "last_used_at"],
    "organize_checkpoints": ["last_full_scan_at", "updated_at"],
    "assistant_conversations": ["created_at", "updated_at", "last_message_at"],
    "assistant_messages": ["created_at", "updated_at"],
    "assistant_runs": ["started_at", "completed_at", "created_at"],
}
UTC_COLUMNS = {"upload_reservations": ["created_at", "expires_at", "cleaned_at"]}


def upgrade():
    connection = op.get_bind()
    for columns, zone in ((BEIJING_COLUMNS, "Asia/Shanghai"), (UTC_COLUMNS, "UTC")):
        for table, names in columns.items():
            for name in names:
                row = connection.execute(text("""SELECT data_type, column_default
                    FROM information_schema.columns WHERE table_schema=current_schema()
                    AND table_name=:table AND column_name=:name"""),
                    {"table": table, "name": name}).one()
                if row.data_type not in ("timestamp without time zone", "timestamp with time zone"):
                    raise RuntimeError(f"Unexpected timestamp type for {table}.{name}")
                target = f'"{table}" ALTER COLUMN "{name}"'
                if row.column_default is not None:
                    op.execute(f'ALTER TABLE {target} DROP DEFAULT')
                if row.data_type == "timestamp without time zone":
                    op.execute(f'ALTER TABLE {target} TYPE TIMESTAMPTZ USING "{name}" AT TIME ZONE \'{zone}\'')
                if row.column_default is not None:
                    op.execute(f'ALTER TABLE {target} SET DEFAULT now()')


def downgrade():
    raise RuntimeError("Restore a verified backup instead of discarding timestamp timezone semantics")
