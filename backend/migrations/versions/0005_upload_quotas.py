"""Durable upload reservations and UTC daily admission limits."""
from alembic import op

revision = '0005_upload_quotas'
down_revision = '0004_hash_only_mcp'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE upload_reservations (
        id VARCHAR(128) PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        size BIGINT NOT NULL CHECK (size >= 0), kind VARCHAR(20) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        created_at TIMESTAMP NOT NULL, expires_at TIMESTAMP NOT NULL, cleaned_at TIMESTAMP,
        details JSON NOT NULL DEFAULT '{}'
    )''')
    op.execute('CREATE INDEX idx_upload_user_active ON upload_reservations (user_id,status,expires_at)')
    op.execute('CREATE INDEX idx_upload_workspace_active ON upload_reservations (workspace_id,status,expires_at)')
    op.execute('''CREATE TABLE upload_daily_usage (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        day DATE NOT NULL, attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        bytes BIGINT NOT NULL DEFAULT 0 CHECK (bytes >= 0), PRIMARY KEY (user_id,day)
    )''')


def downgrade():
    raise RuntimeError('Upload quota history and reservations must not be discarded')
