"""Align migrated and historical native-enum user roles with the ORM."""
from alembic import op

revision = '0006_user_role_type'
down_revision = '0005_upload_quotas'
branch_labels = None
depends_on = None


def upgrade():
    # Historical ORM-created deployments may use user_roles, while the
    # immutable SQL baseline uses VARCHAR(10). Preserve labels and nulls;
    # remove an enum-typed default before converting, then restore it.
    op.execute('ALTER TABLE users ALTER COLUMN role DROP DEFAULT')
    op.execute('ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(10) USING role::text')
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'common'")


def downgrade():
    raise RuntimeError('Restore a verified backup instead of reintroducing incompatible native role types')
