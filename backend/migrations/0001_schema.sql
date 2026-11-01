--
-- PostgreSQL database dump
--


-- Dumped from database version 15.19 (Debian 15.19-1.pgdg12+2)
-- Dumped by pg_dump version 15.19 (Debian 15.19-1.pgdg12+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET search_path = public;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: bump_sql_cache_revisions(); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: bump_workspace_search_revision(); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: cancel_changed_runtime_operation(); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: cancel_deleted_runtime_operation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cancel_deleted_runtime_operation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            UPDATE runtime_operations SET state='cancelled', next_attempt_at=now()
            WHERE runtime_id=OLD.id AND state IN ('preparing','switched');
            RETURN OLD;
        END $$;


--
-- Name: cancel_member_runtime_operation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cancel_member_runtime_operation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            UPDATE runtime_operations SET state='cancelled', next_attempt_at=now()
            WHERE workspace_id=OLD.workspace_id AND user_id=OLD.user_id AND state IN ('preparing','switched');
            RETURN OLD;
        END $$;


--
-- Name: retire_detached_completed_generation(); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: stage_deleted_file_object(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.stage_deleted_file_object() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            INSERT INTO file_object_deletions(object_name) VALUES (OLD.file_path);
            RETURN OLD;
        END
        $$;


--
-- Name: stage_detached_runtime_container(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.stage_detached_runtime_container() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            INSERT INTO runtime_container_deletions(container_id) VALUES (OLD.container_id);
            RETURN OLD;
        END $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: assistant_conversations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_conversations (
    id integer NOT NULL,
    workspace_id integer NOT NULL,
    user_id integer NOT NULL,
    mode character varying(16) DEFAULT 'fast'::character varying NOT NULL,
    title character varying(255),
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    source_conversation_id integer,
    opencode_runtime_id integer,
    opencode_session_id character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_message_at timestamp with time zone,
    CONSTRAINT ck_assistant_conversation_mode CHECK (((mode)::text = ANY ((ARRAY['fast'::character varying, 'expert'::character varying])::text[]))),
    CONSTRAINT ck_assistant_conversation_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'archived'::character varying])::text[])))
);


--
-- Name: assistant_conversations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assistant_conversations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: assistant_conversations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assistant_conversations_id_seq OWNED BY public.assistant_conversations.id;


--
-- Name: assistant_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_messages (
    id integer NOT NULL,
    conversation_id integer NOT NULL,
    role character varying(16) NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    status character varying(16) DEFAULT 'completed'::character varying NOT NULL,
    provider_message_id character varying(128),
    metadata_json text,
    usage_json text,
    sequence integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assistant_message_role CHECK (((role)::text = ANY ((ARRAY['user'::character varying, 'assistant'::character varying, 'system'::character varying])::text[]))),
    CONSTRAINT ck_assistant_message_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'streaming'::character varying, 'completed'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: assistant_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assistant_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: assistant_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assistant_messages_id_seq OWNED BY public.assistant_messages.id;


--
-- Name: assistant_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_runs (
    id integer NOT NULL,
    conversation_id integer NOT NULL,
    user_message_id integer,
    assistant_message_id integer,
    engine character varying(16) NOT NULL,
    status character varying(24) DEFAULT 'pending'::character varying NOT NULL,
    request_id character varying(128),
    opencode_session_id character varying(128),
    cancel_requested boolean DEFAULT false NOT NULL,
    error_code character varying(64),
    error_message text,
    metadata_json text,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    runtime_lease_token character varying(32),
    runtime_lease_runtime_id integer,
    runtime_lease_container_id character varying(64),
    CONSTRAINT ck_assistant_run_engine CHECK (((engine)::text = ANY ((ARRAY['fast'::character varying, 'expert'::character varying])::text[]))),
    CONSTRAINT ck_assistant_run_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'waiting_permission'::character varying, 'completed'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])))
);

--
-- Name: assistant_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_permissions (
    run_id integer NOT NULL,
    permission_id character varying(128) NOT NULL,
    state character varying(16) NOT NULL,
    details_json text NOT NULL,
    response character varying(16),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    answered_at timestamp with time zone,
    dispatched_at timestamp with time zone,
    CONSTRAINT ck_assistant_permission_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'answered'::character varying, 'dispatched'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT ck_assistant_permission_response CHECK ((((state)::text = 'pending'::text) AND (response IS NULL)) OR (((state)::text = ANY ((ARRAY['answered'::character varying, 'dispatched'::character varying])::text[])) AND (response IS NOT NULL) AND ((response)::text = ANY ((ARRAY['once'::character varying, 'reject'::character varying])::text[]))) OR (((state)::text = 'cancelled'::text) AND ((response IS NULL) OR ((response)::text = ANY ((ARRAY['once'::character varying, 'reject'::character varying])::text[])))))
);


--
-- Name: assistant_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assistant_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: assistant_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assistant_runs_id_seq OWNED BY public.assistant_runs.id;


--
-- Name: assistant_runtime_aborts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_runtime_aborts (
    run_id integer NOT NULL,
    workspace_id integer NOT NULL,
    user_id integer NOT NULL,
    runtime_id integer NOT NULL,
    container_id character varying(64) NOT NULL,
    generation character varying(32),
    session_id character varying(128),
    lease_token character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error_type character varying(96)
);


--
-- Name: file_change_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_change_events (
    id integer NOT NULL,
    workspace_id integer NOT NULL,
    entity_type character varying(20) NOT NULL,
    entity_id integer NOT NULL,
    action character varying(32) NOT NULL,
    old_parent_id integer,
    new_parent_id integer,
    old_name character varying(255),
    new_name character varying(255),
    payload text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: file_change_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.file_change_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: file_change_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.file_change_events_id_seq OWNED BY public.file_change_events.id;


--
-- Name: file_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_chunks (
    id integer NOT NULL,
    file_id integer NOT NULL,
    workspace_id integer NOT NULL,
    chunk_index integer NOT NULL,
    page_number integer,
    content text NOT NULL,
    vector_info public.vector(1024),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: file_chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.file_chunks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: file_chunks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.file_chunks_id_seq OWNED BY public.file_chunks.id;


--
-- Name: file_object_deletions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_object_deletions (
    id integer NOT NULL,
    object_name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error_type character varying(96)
);


--
-- Name: file_object_deletions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.file_object_deletions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: file_object_deletions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.file_object_deletions_id_seq OWNED BY public.file_object_deletions.id;


--
-- Name: file_task_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_task_outbox (
    id integer NOT NULL,
    file_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    delivered_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error_type character varying(96)
);


--
-- Name: file_task_outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.file_task_outbox_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: file_task_outbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.file_task_outbox_id_seq OWNED BY public.file_task_outbox.id;


--
-- Name: files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.files (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    file_path character varying(512) NOT NULL,
    file_size bigint,
    mime_type character varying(255),
    content_hash character varying(64),
    status character varying(20) DEFAULT 'pending'::character varying,
    vector_info public.vector(1024),
    description character varying(4096),
    workspace_id integer NOT NULL,
    uploader_id integer,
    parent_id integer,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: files_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.files_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: files_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.files_id_seq OWNED BY public.files.id;


--
-- Name: folder; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.folder (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    parent_id integer,
    workspace_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: folder_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.folder_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: folder_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.folder_id_seq OWNED BY public.folder.id;


--
-- Name: inbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inbox (
    id integer NOT NULL,
    user_id integer NOT NULL,
    title character varying(255),
    content text NOT NULL,
    is_read boolean DEFAULT false,
    type character varying(50) DEFAULT 'system'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    is_deleted boolean DEFAULT false
);


--
-- Name: inbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.inbox_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: inbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.inbox_id_seq OWNED BY public.inbox.id;


--
-- Name: mcp_audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_audit_logs (
    id integer NOT NULL,
    request_id character varying(64) NOT NULL,
    runtime_id integer,
    workspace_id integer,
    user_id integer,
    role character varying(20),
    actor_type character varying(20) DEFAULT 'user'::character varying NOT NULL,
    tool_name character varying(100) NOT NULL,
    arguments_summary text,
    entity_id integer,
    success boolean DEFAULT false NOT NULL,
    error_type character varying(100),
    duration_ms integer,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: mcp_audit_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_audit_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_audit_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_audit_logs_id_seq OWNED BY public.mcp_audit_logs.id;


--
-- Name: mcp_runtime_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_runtime_tokens (
    id integer NOT NULL,
    runtime_id integer NOT NULL,
    user_id integer NOT NULL,
    workspace_id integer NOT NULL,
    jti character varying(64) NOT NULL,
    token_hash character varying(64) NOT NULL,
    token_preview character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);


--
-- Name: mcp_runtime_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_runtime_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_runtime_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_runtime_tokens_id_seq OWNED BY public.mcp_runtime_tokens.id;


--
-- Name: mcp_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_tokens (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name character varying(80) DEFAULT 'MCP Token'::character varying NOT NULL,
    token_hash character varying(64) NOT NULL,
    token_preview character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);


--
-- Name: mcp_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_tokens_id_seq OWNED BY public.mcp_tokens.id;


--
-- Name: opencode_runtimes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.opencode_runtimes (
    id integer NOT NULL,
    workspace_id integer NOT NULL,
    user_id integer NOT NULL,
    container_id character varying(64),
    status character varying(20) DEFAULT 'stopped'::character varying NOT NULL,
    error_message text,
    config_version integer DEFAULT 0 NOT NULL,
    last_started_at timestamp with time zone,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: opencode_runtimes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.opencode_runtimes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: opencode_runtimes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.opencode_runtimes_id_seq OWNED BY public.opencode_runtimes.id;


--
-- Name: organize_checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organize_checkpoints (
    workspace_id integer NOT NULL,
    last_event_id integer DEFAULT 0 NOT NULL,
    last_full_scan_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: organize_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organize_tasks (
    id character varying(64) NOT NULL,
    workspace_id integer NOT NULL,
    user_id integer NOT NULL,
    state character varying(20) DEFAULT 'queued'::character varying NOT NULL,
    execution_token character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    delivered_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error_type character varying(96),
    finished_at timestamp with time zone
);


--
-- Name: runtime_container_deletions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.runtime_container_deletions (
    id integer NOT NULL,
    container_id character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error_type character varying(96)
);


--
-- Name: runtime_container_deletions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.runtime_container_deletions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: runtime_container_deletions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.runtime_container_deletions_id_seq OWNED BY public.runtime_container_deletions.id;


--
-- Name: runtime_operations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.runtime_operations (
    generation character varying(32) NOT NULL,
    workspace_id integer NOT NULL,
    user_id integer NOT NULL,
    runtime_id integer NOT NULL,
    previous_container_id character varying(64),
    candidate_container_id character varying(64),
    state character varying(20) DEFAULT 'preparing'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error_type character varying(96),
    CONSTRAINT ck_runtime_operation_state CHECK (((state)::text = ANY ((ARRAY['preparing'::character varying, 'switched'::character varying, 'complete'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: shares; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shares (
    id integer NOT NULL,
    token character varying(512) NOT NULL,
    file_id integer NOT NULL,
    user_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone
);


--
-- Name: shares_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shares_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shares_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shares_id_seq OWNED BY public.shares.id;


--
-- Name: sql_cache_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sql_cache_revisions (
    scope character varying(160) NOT NULL,
    revision bigint DEFAULT 0 NOT NULL
);


--
-- Name: sys_dict; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sys_dict (
    id integer NOT NULL,
    key character varying(255) NOT NULL,
    value character varying(2048),
    des character varying(255),
    enable boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: sys_dict_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sys_dict_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sys_dict_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sys_dict_id_seq OWNED BY public.sys_dict.id;


--
-- Name: token_usage_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_usage_logs (
    id integer NOT NULL,
    user_id integer NOT NULL,
    action character varying(50) NOT NULL,
    model_name character varying(255),
    prompt_tokens integer DEFAULT 0,
    completion_tokens integer DEFAULT 0,
    total_tokens integer DEFAULT 0,
    query_summary character varying(200),
    extra_info text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: token_usage_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.token_usage_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: token_usage_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.token_usage_logs_id_seq OWNED BY public.token_usage_logs.id;


--
-- Name: upload_daily_usage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.upload_daily_usage (
    user_id integer NOT NULL,
    day date NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    bytes bigint DEFAULT 0 NOT NULL,
    CONSTRAINT upload_daily_usage_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT upload_daily_usage_bytes_check CHECK ((bytes >= 0))
);


--
-- Name: upload_reservations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.upload_reservations (
    id character varying(128) NOT NULL,
    user_id integer NOT NULL,
    workspace_id integer NOT NULL,
    size bigint NOT NULL,
    kind character varying(20) NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    cleaned_at timestamp with time zone,
    details json DEFAULT '{}'::json NOT NULL,
    CONSTRAINT upload_reservations_size_check CHECK ((size >= 0))
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    username character varying(80) NOT NULL,
    password_hash character varying(1024) NOT NULL,
    role character varying(10) DEFAULT 'common'::character varying,
    avatar character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    total_prompt_tokens bigint DEFAULT 0,
    total_completion_tokens bigint DEFAULT 0,
    total_tokens bigint DEFAULT 0,
    last_active_at timestamp with time zone,
    auth_version integer DEFAULT 0 NOT NULL
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: workspace_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_members (
    id integer NOT NULL,
    workspace_id integer NOT NULL,
    user_id integer NOT NULL,
    role character varying(20) DEFAULT 'viewer'::character varying NOT NULL,
    invited_by integer,
    joined_at timestamp with time zone DEFAULT now()
);


--
-- Name: workspace_members_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workspace_members_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: workspace_members_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workspace_members_id_seq OWNED BY public.workspace_members.id;


--
-- Name: workspaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspaces (
    id integer NOT NULL,
    name character varying(128) NOT NULL,
    description character varying(512),
    owner_id integer NOT NULL,
    container_id character varying(64),
    status character varying(20) DEFAULT 'stopped'::character varying NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    search_cache_revision bigint DEFAULT 0 NOT NULL
);


--
-- Name: workspaces_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workspaces_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: workspaces_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workspaces_id_seq OWNED BY public.workspaces.id;


--
-- Name: assistant_conversations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_conversations ALTER COLUMN id SET DEFAULT nextval('public.assistant_conversations_id_seq'::regclass);


--
-- Name: assistant_messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_messages ALTER COLUMN id SET DEFAULT nextval('public.assistant_messages_id_seq'::regclass);


--
-- Name: assistant_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_runs ALTER COLUMN id SET DEFAULT nextval('public.assistant_runs_id_seq'::regclass);


--
-- Name: file_change_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_change_events ALTER COLUMN id SET DEFAULT nextval('public.file_change_events_id_seq'::regclass);


--
-- Name: file_chunks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_chunks ALTER COLUMN id SET DEFAULT nextval('public.file_chunks_id_seq'::regclass);


--
-- Name: file_object_deletions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_object_deletions ALTER COLUMN id SET DEFAULT nextval('public.file_object_deletions_id_seq'::regclass);


--
-- Name: file_task_outbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_task_outbox ALTER COLUMN id SET DEFAULT nextval('public.file_task_outbox_id_seq'::regclass);


--
-- Name: files id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.files ALTER COLUMN id SET DEFAULT nextval('public.files_id_seq'::regclass);


--
-- Name: folder id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folder ALTER COLUMN id SET DEFAULT nextval('public.folder_id_seq'::regclass);


--
-- Name: inbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbox ALTER COLUMN id SET DEFAULT nextval('public.inbox_id_seq'::regclass);


--
-- Name: mcp_audit_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_audit_logs ALTER COLUMN id SET DEFAULT nextval('public.mcp_audit_logs_id_seq'::regclass);


--
-- Name: mcp_runtime_tokens id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_runtime_tokens ALTER COLUMN id SET DEFAULT nextval('public.mcp_runtime_tokens_id_seq'::regclass);


--
-- Name: mcp_tokens id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tokens ALTER COLUMN id SET DEFAULT nextval('public.mcp_tokens_id_seq'::regclass);


--
-- Name: opencode_runtimes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opencode_runtimes ALTER COLUMN id SET DEFAULT nextval('public.opencode_runtimes_id_seq'::regclass);


--
-- Name: runtime_container_deletions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_container_deletions ALTER COLUMN id SET DEFAULT nextval('public.runtime_container_deletions_id_seq'::regclass);


--
-- Name: shares id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shares ALTER COLUMN id SET DEFAULT nextval('public.shares_id_seq'::regclass);


--
-- Name: sys_dict id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sys_dict ALTER COLUMN id SET DEFAULT nextval('public.sys_dict_id_seq'::regclass);


--
-- Name: token_usage_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_usage_logs ALTER COLUMN id SET DEFAULT nextval('public.token_usage_logs_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: workspace_members id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members ALTER COLUMN id SET DEFAULT nextval('public.workspace_members_id_seq'::regclass);


--
-- Name: workspaces id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces ALTER COLUMN id SET DEFAULT nextval('public.workspaces_id_seq'::regclass);


--
-- Name: assistant_conversations assistant_conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_conversations
    ADD CONSTRAINT assistant_conversations_pkey PRIMARY KEY (id);


--
-- Name: assistant_messages assistant_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_messages
    ADD CONSTRAINT assistant_messages_pkey PRIMARY KEY (id);


--
-- Name: assistant_runs assistant_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_runs
    ADD CONSTRAINT assistant_runs_pkey PRIMARY KEY (id);

--
-- Name: assistant_permissions assistant_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_permissions
    ADD CONSTRAINT assistant_permissions_pkey PRIMARY KEY (run_id, permission_id);

--
-- Name: assistant_runtime_aborts assistant_runtime_aborts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_runtime_aborts
    ADD CONSTRAINT assistant_runtime_aborts_pkey PRIMARY KEY (run_id);


--
-- Name: file_change_events file_change_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_change_events
    ADD CONSTRAINT file_change_events_pkey PRIMARY KEY (id);


--
-- Name: file_chunks file_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_chunks
    ADD CONSTRAINT file_chunks_pkey PRIMARY KEY (id);


--
-- Name: file_object_deletions file_object_deletions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_object_deletions
    ADD CONSTRAINT file_object_deletions_pkey PRIMARY KEY (id);


--
-- Name: file_task_outbox file_task_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_task_outbox
    ADD CONSTRAINT file_task_outbox_pkey PRIMARY KEY (id);


--
-- Name: files files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.files
    ADD CONSTRAINT files_pkey PRIMARY KEY (id);


--
-- Name: folder folder_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folder
    ADD CONSTRAINT folder_pkey PRIMARY KEY (id);


--
-- Name: inbox inbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbox
    ADD CONSTRAINT inbox_pkey PRIMARY KEY (id);


--
-- Name: mcp_audit_logs mcp_audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_audit_logs
    ADD CONSTRAINT mcp_audit_logs_pkey PRIMARY KEY (id);


--
-- Name: mcp_runtime_tokens mcp_runtime_tokens_jti_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_runtime_tokens
    ADD CONSTRAINT mcp_runtime_tokens_jti_key UNIQUE (jti);


--
-- Name: mcp_runtime_tokens mcp_runtime_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_runtime_tokens
    ADD CONSTRAINT mcp_runtime_tokens_pkey PRIMARY KEY (id);


--
-- Name: mcp_runtime_tokens mcp_runtime_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_runtime_tokens
    ADD CONSTRAINT mcp_runtime_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: mcp_tokens mcp_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tokens
    ADD CONSTRAINT mcp_tokens_pkey PRIMARY KEY (id);


--
-- Name: mcp_tokens mcp_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tokens
    ADD CONSTRAINT mcp_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: opencode_runtimes opencode_runtimes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opencode_runtimes
    ADD CONSTRAINT opencode_runtimes_pkey PRIMARY KEY (id);


--
-- Name: organize_checkpoints organize_checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organize_checkpoints
    ADD CONSTRAINT organize_checkpoints_pkey PRIMARY KEY (workspace_id);


--
-- Name: organize_tasks organize_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organize_tasks
    ADD CONSTRAINT organize_tasks_pkey PRIMARY KEY (id);


--
-- Name: runtime_container_deletions runtime_container_deletions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_container_deletions
    ADD CONSTRAINT runtime_container_deletions_pkey PRIMARY KEY (id);


--
-- Name: runtime_operations runtime_operations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_operations
    ADD CONSTRAINT runtime_operations_pkey PRIMARY KEY (generation);


--
-- Name: shares shares_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shares
    ADD CONSTRAINT shares_pkey PRIMARY KEY (id);


--
-- Name: shares shares_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shares
    ADD CONSTRAINT shares_token_key UNIQUE (token);


--
-- Name: sql_cache_revisions sql_cache_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sql_cache_revisions
    ADD CONSTRAINT sql_cache_revisions_pkey PRIMARY KEY (scope);


--
-- Name: sys_dict sys_dict_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sys_dict
    ADD CONSTRAINT sys_dict_key_key UNIQUE (key);


--
-- Name: sys_dict sys_dict_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sys_dict
    ADD CONSTRAINT sys_dict_pkey PRIMARY KEY (id);


--
-- Name: token_usage_logs token_usage_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_usage_logs
    ADD CONSTRAINT token_usage_logs_pkey PRIMARY KEY (id);


--
-- Name: upload_daily_usage upload_daily_usage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_daily_usage
    ADD CONSTRAINT upload_daily_usage_pkey PRIMARY KEY (user_id, day);


--
-- Name: upload_reservations upload_reservations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_reservations
    ADD CONSTRAINT upload_reservations_pkey PRIMARY KEY (id);


--
-- Name: assistant_messages uq_assistant_message_sequence; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_messages
    ADD CONSTRAINT uq_assistant_message_sequence UNIQUE (conversation_id, sequence);


--
-- Name: file_chunks uq_file_chunks_file_index; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_chunks
    ADD CONSTRAINT uq_file_chunks_file_index UNIQUE (file_id, chunk_index);


--
-- Name: opencode_runtimes uq_opencode_runtime_workspace_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opencode_runtimes
    ADD CONSTRAINT uq_opencode_runtime_workspace_user UNIQUE (workspace_id, user_id);


--
-- Name: workspace_members uq_workspace_member; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT uq_workspace_member UNIQUE (workspace_id, user_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: workspace_members workspace_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_pkey PRIMARY KEY (id);


--
-- Name: workspaces workspaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_pkey PRIMARY KEY (id);


--
-- Name: file_chunk_vector_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_chunk_vector_idx ON public.file_chunks USING hnsw (vector_info public.vector_cosine_ops);


--
-- Name: file_vector_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_vector_idx ON public.files USING hnsw (vector_info public.vector_cosine_ops);


--
-- Name: idx_assistant_conversations_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assistant_conversations_owner ON public.assistant_conversations USING btree (workspace_id, user_id, mode);


--
-- Name: idx_assistant_conversations_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assistant_conversations_updated ON public.assistant_conversations USING btree (workspace_id, user_id, updated_at);


--
-- Name: idx_assistant_messages_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assistant_messages_conversation ON public.assistant_messages USING btree (conversation_id, sequence);


--
-- Name: idx_assistant_runs_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assistant_runs_active ON public.assistant_runs USING btree (status, engine);


--
-- Name: idx_assistant_runs_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assistant_runs_conversation ON public.assistant_runs USING btree (conversation_id, created_at);
--
-- Name: uq_assistant_permission_open_run; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_assistant_permission_open_run ON public.assistant_permissions USING btree (run_id) WHERE (state IN ('pending', 'answered'));


--
-- Name: idx_file_change_events_ws_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_file_change_events_ws_created ON public.file_change_events USING btree (workspace_id, created_at);


--
-- Name: idx_file_change_events_ws_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_file_change_events_ws_id ON public.file_change_events USING btree (workspace_id, id);


--
-- Name: idx_file_chunks_ws_file; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_file_chunks_ws_file ON public.file_chunks USING btree (workspace_id, file_id);


--
-- Name: idx_files_content_hash_size; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_files_content_hash_size ON public.files USING btree (content_hash, file_size);


--
-- Name: idx_files_ws_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_files_ws_parent ON public.files USING btree (workspace_id, parent_id);


--
-- Name: idx_files_ws_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_files_ws_status ON public.files USING btree (workspace_id, status);


--
-- Name: idx_folder_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_folder_workspace_id ON public.folder USING btree (workspace_id);


--
-- Name: idx_folder_ws_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_folder_ws_parent ON public.folder USING btree (workspace_id, parent_id);


--
-- Name: idx_mcp_audit_runtime_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_audit_runtime_created ON public.mcp_audit_logs USING btree (runtime_id, created_at);


--
-- Name: idx_mcp_audit_user_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_audit_user_created ON public.mcp_audit_logs USING btree (user_id, created_at);


--
-- Name: idx_mcp_audit_workspace_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_audit_workspace_created ON public.mcp_audit_logs USING btree (workspace_id, created_at);


--
-- Name: idx_mcp_runtime_tokens_runtime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_runtime_tokens_runtime ON public.mcp_runtime_tokens USING btree (runtime_id);


--
-- Name: idx_mcp_runtime_tokens_user_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_runtime_tokens_user_workspace ON public.mcp_runtime_tokens USING btree (user_id, workspace_id);


--
-- Name: idx_mcp_tokens_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_tokens_token_hash ON public.mcp_tokens USING btree (token_hash);


--
-- Name: idx_mcp_tokens_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_tokens_user_id ON public.mcp_tokens USING btree (user_id);


--
-- Name: idx_opencode_runtime_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_opencode_runtime_user ON public.opencode_runtimes USING btree (user_id);


--
-- Name: idx_opencode_runtime_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_opencode_runtime_workspace ON public.opencode_runtimes USING btree (workspace_id);


--
-- Name: idx_token_usage_logs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_logs_created_at ON public.token_usage_logs USING btree (user_id, created_at);


--
-- Name: idx_token_usage_logs_user_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_logs_user_action ON public.token_usage_logs USING btree (user_id, action);


--
-- Name: idx_token_usage_logs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_logs_user_id ON public.token_usage_logs USING btree (user_id);


--
-- Name: idx_upload_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_upload_user_active ON public.upload_reservations USING btree (user_id, status, expires_at);


--
-- Name: idx_upload_workspace_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_upload_workspace_active ON public.upload_reservations USING btree (workspace_id, status, expires_at);


--
-- Name: idx_wm_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wm_user_id ON public.workspace_members USING btree (user_id);


--
-- Name: idx_wm_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wm_workspace_id ON public.workspace_members USING btree (workspace_id);


--
-- Name: idx_workspaces_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspaces_owner_id ON public.workspaces USING btree (owner_id);


--
-- Name: ix_assistant_runtime_aborts_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_runtime_aborts_pending ON public.assistant_runtime_aborts USING btree (workspace_id, user_id) WHERE (completed_at IS NULL);


--
-- Name: ix_file_object_deletions_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_file_object_deletions_pending ON public.file_object_deletions USING btree (id) WHERE (completed_at IS NULL);


--
-- Name: ix_file_task_outbox_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_file_task_outbox_pending ON public.file_task_outbox USING btree (id) WHERE (delivered_at IS NULL);


--
-- Name: ix_files_object_reference; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_files_object_reference ON public.files USING btree (file_path);


--
-- Name: ix_runtime_container_deletions_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runtime_container_deletions_pending ON public.runtime_container_deletions USING btree (id) WHERE (completed_at IS NULL);


--
-- Name: ix_runtime_operations_candidate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runtime_operations_candidate ON public.runtime_operations USING btree (candidate_container_id);


--
-- Name: ix_runtime_operations_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runtime_operations_pending ON public.runtime_operations USING btree (next_attempt_at, generation) WHERE (finished_at IS NULL);


--
-- Name: uq_mcp_tokens_active_user; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_mcp_tokens_active_user ON public.mcp_tokens USING btree (user_id) WHERE (revoked_at IS NULL);


--
-- Name: uq_organize_active_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_organize_active_workspace ON public.organize_tasks USING btree (workspace_id) WHERE ((state)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]));


--
-- Name: uq_runtime_operation_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_runtime_operation_active ON public.runtime_operations USING btree (runtime_id) WHERE ((state)::text = ANY ((ARRAY['preparing'::character varying, 'switched'::character varying])::text[]));


--
-- Name: opencode_runtimes cancel_changed_runtime_operation; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER cancel_changed_runtime_operation AFTER UPDATE OF container_id, status ON public.opencode_runtimes FOR EACH ROW EXECUTE FUNCTION public.cancel_changed_runtime_operation();


--
-- Name: opencode_runtimes cancel_deleted_runtime_operation; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER cancel_deleted_runtime_operation AFTER DELETE ON public.opencode_runtimes FOR EACH ROW EXECUTE FUNCTION public.cancel_deleted_runtime_operation();


--
-- Name: workspace_members cancel_demoted_member_runtime_operation; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER cancel_demoted_member_runtime_operation AFTER UPDATE OF role ON public.workspace_members FOR EACH ROW WHEN (((new.role)::text <> ALL ((ARRAY['editor'::character varying, 'admin'::character varying])::text[]))) EXECUTE FUNCTION public.cancel_member_runtime_operation();


--
-- Name: workspace_members cancel_removed_member_runtime_operation; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER cancel_removed_member_runtime_operation AFTER DELETE ON public.workspace_members FOR EACH ROW EXECUTE FUNCTION public.cancel_member_runtime_operation();


--
-- Name: files files_search_cache_revision; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER files_search_cache_revision AFTER INSERT OR DELETE OR UPDATE ON public.files FOR EACH ROW EXECUTE FUNCTION public.bump_workspace_search_revision();


--
-- Name: files files_sql_cache_revision; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER files_sql_cache_revision AFTER INSERT OR DELETE OR UPDATE ON public.files FOR EACH ROW EXECUTE FUNCTION public.bump_sql_cache_revisions();


--
-- Name: folder folder_sql_cache_revision; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER folder_sql_cache_revision AFTER INSERT OR DELETE OR UPDATE ON public.folder FOR EACH ROW EXECUTE FUNCTION public.bump_sql_cache_revisions();


--
-- Name: opencode_runtimes retire_opencode_runtimes_generation_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER retire_opencode_runtimes_generation_delete AFTER DELETE ON public.opencode_runtimes FOR EACH ROW EXECUTE FUNCTION public.retire_detached_completed_generation();


--
-- Name: opencode_runtimes retire_opencode_runtimes_generation_detach; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER retire_opencode_runtimes_generation_detach AFTER UPDATE OF container_id ON public.opencode_runtimes FOR EACH ROW WHEN (((old.container_id)::text IS DISTINCT FROM (new.container_id)::text)) EXECUTE FUNCTION public.retire_detached_completed_generation();


--
-- Name: workspaces retire_workspaces_generation_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER retire_workspaces_generation_delete AFTER DELETE ON public.workspaces FOR EACH ROW EXECUTE FUNCTION public.retire_detached_completed_generation();


--
-- Name: workspaces retire_workspaces_generation_detach; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER retire_workspaces_generation_detach AFTER UPDATE OF container_id ON public.workspaces FOR EACH ROW WHEN (((old.container_id)::text IS DISTINCT FROM (new.container_id)::text)) EXECUTE FUNCTION public.retire_detached_completed_generation();


--
-- Name: files stage_file_object_cleanup; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER stage_file_object_cleanup AFTER DELETE ON public.files FOR EACH ROW EXECUTE FUNCTION public.stage_deleted_file_object();


--
-- Name: opencode_runtimes stage_opencode_runtimes_container_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER stage_opencode_runtimes_container_delete AFTER DELETE ON public.opencode_runtimes FOR EACH ROW WHEN ((old.container_id IS NOT NULL)) EXECUTE FUNCTION public.stage_detached_runtime_container();


--
-- Name: opencode_runtimes stage_opencode_runtimes_container_detach; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER stage_opencode_runtimes_container_detach AFTER UPDATE OF container_id ON public.opencode_runtimes FOR EACH ROW WHEN (((old.container_id IS NOT NULL) AND ((old.container_id)::text IS DISTINCT FROM (new.container_id)::text))) EXECUTE FUNCTION public.stage_detached_runtime_container();


--
-- Name: workspaces stage_workspaces_container_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER stage_workspaces_container_delete AFTER DELETE ON public.workspaces FOR EACH ROW WHEN ((old.container_id IS NOT NULL)) EXECUTE FUNCTION public.stage_detached_runtime_container();


--
-- Name: workspaces stage_workspaces_container_detach; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER stage_workspaces_container_detach AFTER UPDATE OF container_id ON public.workspaces FOR EACH ROW WHEN (((old.container_id IS NOT NULL) AND ((old.container_id)::text IS DISTINCT FROM (new.container_id)::text))) EXECUTE FUNCTION public.stage_detached_runtime_container();




--
-- Name: users users_sql_cache_revision; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER users_sql_cache_revision AFTER INSERT OR DELETE OR UPDATE ON public.users FOR EACH ROW EXECUTE FUNCTION public.bump_sql_cache_revisions();


--
-- Name: assistant_conversations assistant_conversations_opencode_runtime_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_conversations
    ADD CONSTRAINT assistant_conversations_opencode_runtime_id_fkey FOREIGN KEY (opencode_runtime_id) REFERENCES public.opencode_runtimes(id) ON DELETE SET NULL;


--
-- Name: assistant_conversations assistant_conversations_source_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_conversations
    ADD CONSTRAINT assistant_conversations_source_conversation_id_fkey FOREIGN KEY (source_conversation_id) REFERENCES public.assistant_conversations(id) ON DELETE SET NULL;


--
-- Name: assistant_conversations assistant_conversations_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_conversations
    ADD CONSTRAINT assistant_conversations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: assistant_conversations assistant_conversations_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_conversations
    ADD CONSTRAINT assistant_conversations_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: assistant_messages assistant_messages_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_messages
    ADD CONSTRAINT assistant_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.assistant_conversations(id) ON DELETE CASCADE;


--
-- Name: assistant_runs assistant_runs_assistant_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_runs
    ADD CONSTRAINT assistant_runs_assistant_message_id_fkey FOREIGN KEY (assistant_message_id) REFERENCES public.assistant_messages(id) ON DELETE SET NULL;


--
-- Name: assistant_runs assistant_runs_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_runs
    ADD CONSTRAINT assistant_runs_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.assistant_conversations(id) ON DELETE CASCADE;


--
-- Name: assistant_runs assistant_runs_user_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_runs
    ADD CONSTRAINT assistant_runs_user_message_id_fkey FOREIGN KEY (user_message_id) REFERENCES public.assistant_messages(id) ON DELETE SET NULL;
--
-- Name: assistant_permissions assistant_permissions_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_permissions
    ADD CONSTRAINT assistant_permissions_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.assistant_runs(id) ON DELETE CASCADE;


--
-- Name: file_change_events file_change_events_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_change_events
    ADD CONSTRAINT file_change_events_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: file_chunks file_chunks_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_chunks
    ADD CONSTRAINT file_chunks_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.files(id) ON DELETE CASCADE;


--
-- Name: file_chunks file_chunks_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_chunks
    ADD CONSTRAINT file_chunks_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: file_task_outbox file_task_outbox_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_task_outbox
    ADD CONSTRAINT file_task_outbox_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.files(id) ON DELETE CASCADE;


--
-- Name: files files_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.files
    ADD CONSTRAINT files_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.folder(id);


--
-- Name: files files_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.files
    ADD CONSTRAINT files_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: files fk_files_uploader_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.files
    ADD CONSTRAINT fk_files_uploader_id_users FOREIGN KEY (uploader_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: mcp_tokens fk_mcp_tokens_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_tokens
    ADD CONSTRAINT fk_mcp_tokens_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: shares fk_shares_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shares
    ADD CONSTRAINT fk_shares_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: workspace_members fk_workspace_members_invited_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT fk_workspace_members_invited_by_users FOREIGN KEY (invited_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: workspaces fk_workspaces_owner_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT fk_workspaces_owner_id_users FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: folder folder_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folder
    ADD CONSTRAINT folder_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.folder(id) ON DELETE CASCADE;


--
-- Name: folder folder_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folder
    ADD CONSTRAINT folder_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: inbox inbox_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbox
    ADD CONSTRAINT inbox_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: mcp_audit_logs mcp_audit_logs_runtime_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_audit_logs
    ADD CONSTRAINT mcp_audit_logs_runtime_id_fkey FOREIGN KEY (runtime_id) REFERENCES public.opencode_runtimes(id) ON DELETE SET NULL;


--
-- Name: mcp_audit_logs mcp_audit_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_audit_logs
    ADD CONSTRAINT mcp_audit_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: mcp_audit_logs mcp_audit_logs_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_audit_logs
    ADD CONSTRAINT mcp_audit_logs_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE SET NULL;


--
-- Name: mcp_runtime_tokens mcp_runtime_tokens_runtime_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_runtime_tokens
    ADD CONSTRAINT mcp_runtime_tokens_runtime_id_fkey FOREIGN KEY (runtime_id) REFERENCES public.opencode_runtimes(id) ON DELETE CASCADE;


--
-- Name: mcp_runtime_tokens mcp_runtime_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_runtime_tokens
    ADD CONSTRAINT mcp_runtime_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: mcp_runtime_tokens mcp_runtime_tokens_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_runtime_tokens
    ADD CONSTRAINT mcp_runtime_tokens_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: opencode_runtimes opencode_runtimes_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opencode_runtimes
    ADD CONSTRAINT opencode_runtimes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: opencode_runtimes opencode_runtimes_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opencode_runtimes
    ADD CONSTRAINT opencode_runtimes_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: organize_checkpoints organize_checkpoints_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organize_checkpoints
    ADD CONSTRAINT organize_checkpoints_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: organize_tasks organize_tasks_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organize_tasks
    ADD CONSTRAINT organize_tasks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: organize_tasks organize_tasks_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organize_tasks
    ADD CONSTRAINT organize_tasks_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: shares shares_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shares
    ADD CONSTRAINT shares_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.files(id) ON DELETE CASCADE;


--
-- Name: token_usage_logs token_usage_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_usage_logs
    ADD CONSTRAINT token_usage_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: upload_daily_usage upload_daily_usage_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_daily_usage
    ADD CONSTRAINT upload_daily_usage_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: upload_reservations upload_reservations_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_reservations
    ADD CONSTRAINT upload_reservations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: upload_reservations upload_reservations_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_reservations
    ADD CONSTRAINT upload_reservations_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: workspace_members workspace_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: workspace_members workspace_members_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--



INSERT INTO public.sys_dict (key, value, des)
VALUES ('site_name', 'SKYCloud', '站点名称（显示在网页标题与 Logo 旁）')
ON CONFLICT (key) DO NOTHING;
