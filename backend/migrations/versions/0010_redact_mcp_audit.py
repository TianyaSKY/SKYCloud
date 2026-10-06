"""Remove legacy raw MCP argument summaries while retaining audit event metadata."""
from alembic import op
from sqlalchemy import text

revision = "0010_redact_mcp_audit"
down_revision = "0009_utc_timestamps"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().execute(text("""UPDATE mcp_audit_logs
        SET arguments_summary = :redacted WHERE arguments_summary IS NOT NULL"""),
        {"redacted": '{"schema":2,"legacy_redacted":true}'})


def downgrade():
    raise RuntimeError("Redacted audit arguments cannot be reconstructed")
