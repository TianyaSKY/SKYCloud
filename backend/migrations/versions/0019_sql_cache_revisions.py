"""Make profile/dictionary/tree cache invalidation part of SQL mutations."""
from alembic import op

revision = '0019_sql_cache_revisions'
down_revision = '0018_search_cache_revision'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE TABLE sql_cache_revisions (scope VARCHAR(160) PRIMARY KEY, revision BIGINT NOT NULL DEFAULT 0)')
    op.execute("""CREATE FUNCTION bump_sql_cache_revisions() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE scopes TEXT[]; old_row JSONB; new_row JSONB; cache_scope TEXT;
    BEGIN
        IF TG_OP <> 'INSERT' THEN old_row := to_jsonb(OLD); END IF;
        IF TG_OP <> 'DELETE' THEN new_row := to_jsonb(NEW); END IF;
        IF TG_TABLE_NAME = 'users' THEN
            scopes := ARRAY['user:profile:' || (old_row->>'id'), 'user:profile:' || (new_row->>'id')];
        ELSIF TG_TABLE_NAME = 'sys_dict' THEN
            scopes := ARRAY['sys_dict:all'];
        ELSE
            scopes := ARRAY['workspace:tree:' || (old_row->>'workspace_id'), 'workspace:tree:' || (new_row->>'workspace_id')];
        END IF;
        FOR cache_scope IN SELECT DISTINCT value FROM unnest(scopes) AS ids(value)
                           WHERE value IS NOT NULL ORDER BY value
        LOOP
            INSERT INTO sql_cache_revisions(scope,revision) VALUES(cache_scope,1)
            ON CONFLICT(scope) DO UPDATE SET revision = sql_cache_revisions.revision + 1;
        END LOOP;
        RETURN NULL;
    END $$""")
    for table in ('users', 'sys_dict', 'folder', 'files'):
        op.execute(f'CREATE TRIGGER {table}_sql_cache_revision AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION bump_sql_cache_revisions()')


def downgrade():
    raise RuntimeError('Restore a verified backup instead of resetting durable SQL cache revisions')
