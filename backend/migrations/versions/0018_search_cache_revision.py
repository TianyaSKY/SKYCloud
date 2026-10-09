"""Fence search cache entries with an authoritative transactional SQL revision."""
from alembic import op

revision = '0018_search_cache_revision'
down_revision = '0017_assistant_abort'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE workspaces ADD COLUMN search_cache_revision BIGINT NOT NULL DEFAULT 0')
    op.execute("""CREATE FUNCTION bump_workspace_search_revision() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE affected INTEGER[]; workspace INTEGER;
    BEGIN
        IF TG_OP = 'INSERT' THEN affected := ARRAY[NEW.workspace_id];
        ELSIF TG_OP = 'DELETE' THEN affected := ARRAY[OLD.workspace_id];
        ELSE affected := ARRAY[OLD.workspace_id,NEW.workspace_id];
        END IF;
        FOR workspace IN SELECT DISTINCT value FROM unnest(affected) AS ids(value)
                         WHERE value IS NOT NULL ORDER BY value
        LOOP
            UPDATE workspaces SET search_cache_revision = search_cache_revision + 1
            WHERE id = workspace;
        END LOOP;
        RETURN NULL;
    END $$""")
    op.execute('CREATE TRIGGER files_search_cache_revision AFTER INSERT OR UPDATE OR DELETE ON files FOR EACH ROW EXECUTE FUNCTION bump_workspace_search_revision()')


def downgrade():
    raise RuntimeError('Restore a verified backup instead of resetting durable search cache revisions')
