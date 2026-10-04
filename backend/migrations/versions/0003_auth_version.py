"""Add revocable Web session versions to accounts."""

from alembic import op

revision = "0003_auth_version"
down_revision = "0002_vector_index"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0")


def downgrade():
    raise RuntimeError("Removing auth_version would restore revoked sessions; restore a verified backup instead")
