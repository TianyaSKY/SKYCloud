"""Capture ordinary, bulk and cascading file deletions in their SQL transaction."""
from alembic import op
from sqlalchemy import text

revision = "0012_object_delete_trigger"
down_revision = "0011_object_deletions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE FUNCTION stage_deleted_file_object() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO file_object_deletions(object_name) VALUES (OLD.file_path);
            RETURN OLD;
        END
        $$""")
    op.execute("""CREATE TRIGGER stage_file_object_cleanup AFTER DELETE ON files
        FOR EACH ROW EXECUTE FUNCTION stage_deleted_file_object()""")

    # File ownership is workspace-scoped; nullable uploader attribution must
    # not block account cascades or destroy files in other workspaces.
    connection = op.get_bind()
    names = connection.execute(text("""SELECT c.conname FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attname='uploader_id'
        WHERE c.conrelid='files'::regclass AND c.confrelid='users'::regclass
        AND c.contype='f' AND c.conkey=ARRAY[a.attnum]""")).scalars().all()
    for name in names:
        quoted = name.replace('"', '""')
        op.execute(f'ALTER TABLE files DROP CONSTRAINT "{quoted}"')
    op.execute("""ALTER TABLE files ADD CONSTRAINT fk_files_uploader_id_users
        FOREIGN KEY (uploader_id) REFERENCES users(id) ON DELETE SET NULL""")


def downgrade():
    raise RuntimeError("Restore a verified backup instead of bypassing pending object cleanup")
