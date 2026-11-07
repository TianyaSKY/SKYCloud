-- PostgreSQL-only functions and triggers. Tables and indexes are defined by the ORM.
CREATE FUNCTION public.bump_sql_cache_revisions() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE scopes TEXT[]; old_row JSONB; new_row JSONB; cache_scope TEXT;
    BEGIN
        IF TG_OP <> 'INSERT' THEN old_row := to_jsonb(OLD); END IF;
        IF TG_OP <> 'DELETE' THEN new_row := to_jsonb(NEW); END IF;
        IF TG_TABLE_NAME = 'users' THEN
            scopes := ARRAY['user:profile:' || (old_row->>'id'), 'user:profile:' || (new_row->>'id')];
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
    END $$;

CREATE FUNCTION public.bump_workspace_search_revision() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
    END $$;

CREATE FUNCTION public.cancel_changed_runtime_operation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            UPDATE runtime_operations SET state='cancelled', next_attempt_at=now()
            WHERE runtime_id=OLD.id AND state IN ('preparing','switched') AND (
                (OLD.container_id IS DISTINCT FROM NEW.container_id AND candidate_container_id IS DISTINCT FROM NEW.container_id)
                OR (OLD.status IS DISTINCT FROM NEW.status AND NEW.status='stopped'));
            RETURN NEW;
        END $$;

CREATE FUNCTION public.cancel_deleted_runtime_operation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            UPDATE runtime_operations SET state='cancelled', next_attempt_at=now()
            WHERE runtime_id=OLD.id AND state IN ('preparing','switched');
            RETURN OLD;
        END $$;

CREATE FUNCTION public.cancel_member_runtime_operation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            UPDATE runtime_operations SET state='cancelled', next_attempt_at=now()
            WHERE workspace_id=OLD.workspace_id AND user_id=OLD.user_id AND state IN ('preparing','switched');
            RETURN OLD;
        END $$;

CREATE FUNCTION public.retire_detached_completed_generation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF OLD.container_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM opencode_runtimes WHERE container_id=OLD.container_id
            ) AND NOT EXISTS (SELECT 1 FROM workspaces WHERE container_id=OLD.container_id) THEN
                UPDATE runtime_operations SET state='cancelled', finished_at=NULL,
                    next_attempt_at=clock_timestamp(), last_error_type=NULL
                WHERE state='complete' AND candidate_container_id=OLD.container_id;
            END IF;
            RETURN OLD;
        END $$;

CREATE FUNCTION public.stage_deleted_file_object() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            INSERT INTO file_object_deletions(object_name) VALUES (OLD.file_path);
            RETURN OLD;
        END
        $$;

CREATE FUNCTION public.stage_detached_runtime_container() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            INSERT INTO runtime_container_deletions(container_id) VALUES (OLD.container_id);
            RETURN OLD;
        END $$;

CREATE TRIGGER cancel_changed_runtime_operation AFTER UPDATE OF container_id, status ON public.opencode_runtimes FOR EACH ROW EXECUTE FUNCTION public.cancel_changed_runtime_operation();
CREATE TRIGGER cancel_deleted_runtime_operation AFTER DELETE ON public.opencode_runtimes FOR EACH ROW EXECUTE FUNCTION public.cancel_deleted_runtime_operation();
CREATE TRIGGER cancel_demoted_member_runtime_operation AFTER UPDATE OF role ON public.workspace_members FOR EACH ROW WHEN (((new.role)::text <> ALL ((ARRAY['editor'::character varying, 'admin'::character varying])::text[]))) EXECUTE FUNCTION public.cancel_member_runtime_operation();
CREATE TRIGGER cancel_removed_member_runtime_operation AFTER DELETE ON public.workspace_members FOR EACH ROW EXECUTE FUNCTION public.cancel_member_runtime_operation();
CREATE TRIGGER files_search_cache_revision AFTER INSERT OR DELETE OR UPDATE ON public.files FOR EACH ROW EXECUTE FUNCTION public.bump_workspace_search_revision();
CREATE TRIGGER files_sql_cache_revision AFTER INSERT OR DELETE OR UPDATE ON public.files FOR EACH ROW EXECUTE FUNCTION public.bump_sql_cache_revisions();
CREATE TRIGGER folder_sql_cache_revision AFTER INSERT OR DELETE OR UPDATE ON public.folder FOR EACH ROW EXECUTE FUNCTION public.bump_sql_cache_revisions();
CREATE TRIGGER retire_opencode_runtimes_generation_delete AFTER DELETE ON public.opencode_runtimes FOR EACH ROW EXECUTE FUNCTION public.retire_detached_completed_generation();
CREATE TRIGGER retire_opencode_runtimes_generation_detach AFTER UPDATE OF container_id ON public.opencode_runtimes FOR EACH ROW WHEN (((old.container_id)::text IS DISTINCT FROM (new.container_id)::text)) EXECUTE FUNCTION public.retire_detached_completed_generation();
CREATE TRIGGER retire_workspaces_generation_delete AFTER DELETE ON public.workspaces FOR EACH ROW EXECUTE FUNCTION public.retire_detached_completed_generation();
CREATE TRIGGER retire_workspaces_generation_detach AFTER UPDATE OF container_id ON public.workspaces FOR EACH ROW WHEN (((old.container_id)::text IS DISTINCT FROM (new.container_id)::text)) EXECUTE FUNCTION public.retire_detached_completed_generation();
CREATE TRIGGER stage_file_object_cleanup AFTER DELETE ON public.files FOR EACH ROW EXECUTE FUNCTION public.stage_deleted_file_object();
CREATE TRIGGER stage_opencode_runtimes_container_delete AFTER DELETE ON public.opencode_runtimes FOR EACH ROW WHEN ((old.container_id IS NOT NULL)) EXECUTE FUNCTION public.stage_detached_runtime_container();
CREATE TRIGGER stage_opencode_runtimes_container_detach AFTER UPDATE OF container_id ON public.opencode_runtimes FOR EACH ROW WHEN (((old.container_id IS NOT NULL) AND ((old.container_id)::text IS DISTINCT FROM (new.container_id)::text))) EXECUTE FUNCTION public.stage_detached_runtime_container();
CREATE TRIGGER stage_workspaces_container_delete AFTER DELETE ON public.workspaces FOR EACH ROW WHEN ((old.container_id IS NOT NULL)) EXECUTE FUNCTION public.stage_detached_runtime_container();
CREATE TRIGGER stage_workspaces_container_detach AFTER UPDATE OF container_id ON public.workspaces FOR EACH ROW WHEN (((old.container_id IS NOT NULL) AND ((old.container_id)::text IS DISTINCT FROM (new.container_id)::text))) EXECUTE FUNCTION public.stage_detached_runtime_container();
CREATE TRIGGER users_sql_cache_revision AFTER INSERT OR DELETE OR UPDATE ON public.users FOR EACH ROW EXECUTE FUNCTION public.bump_sql_cache_revisions();
