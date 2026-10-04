"""Remove persisted external MCP credentials and rotate historical tokens."""

from alembic import op

revision = "0004_hash_only_mcp"
down_revision = "0003_auth_version"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE mcp_tokens SET revoked_at=timezone('Asia/Shanghai', now()) WHERE revoked_at IS NULL")
    op.execute("UPDATE mcp_tokens SET token_preview='sha256:' || substr(token_hash,1,12)")
    op.execute("ALTER TABLE mcp_tokens DROP COLUMN token_value")
    op.execute("CREATE UNIQUE INDEX uq_mcp_tokens_active_user ON mcp_tokens (user_id) WHERE revoked_at IS NULL")


def downgrade():
    raise RuntimeError("Hash-only MCP credentials cannot be restored to plaintext storage")
