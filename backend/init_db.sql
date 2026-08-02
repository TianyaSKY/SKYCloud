-- SKYCloud 数据库初始化 SQL 文件（工作空间机制）
-- 注意：此脚本仅用于初始化空数据库，若表已存在可能会报错或跳过。
-- 建议在执行前清空数据库或确保无冲突。

-- 1. 创建 pgvector 扩展 (如果尚未存在)
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. 创建用户表
CREATE TABLE IF NOT EXISTS users
(
    id                     SERIAL PRIMARY KEY,
    username               VARCHAR(80) UNIQUE NOT NULL,
    password_hash          VARCHAR(1024)      NOT NULL,
    role                   VARCHAR(10) DEFAULT 'common', -- admin, common
    avatar                 VARCHAR(255),
    created_at             TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    total_prompt_tokens    BIGINT DEFAULT 0,
    total_completion_tokens BIGINT DEFAULT 0,
    total_tokens           BIGINT DEFAULT 0,
    last_active_at         TIMESTAMP
);

-- 3. 创建工作空间表（文件/文件夹的归属实体）
CREATE TABLE IF NOT EXISTS workspaces
(
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    description VARCHAR(512),
    owner_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    container_id VARCHAR(64),
    status       VARCHAR(20) NOT NULL DEFAULT 'stopped', -- stopped / running / error
    error_message TEXT,
    created_at  TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    updated_at  TIMESTAMP DEFAULT timezone('Asia/Shanghai', now())
);
CREATE INDEX IF NOT EXISTS idx_workspaces_owner_id ON workspaces (owner_id);
-- 兼容已初始化实例：为既有协作空间补上 OpenCode Docker 生命周期字段。
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS container_id VARCHAR(64);
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'stopped';
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS error_message TEXT;

-- 4. 创建工作空间成员表（用户与空间的多对多关系及角色）
CREATE TABLE IF NOT EXISTS workspace_members
(
    id           SERIAL PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role         VARCHAR(20) NOT NULL DEFAULT 'viewer', -- admin / editor / viewer
    invited_by   INTEGER REFERENCES users (id),
    joined_at    TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    CONSTRAINT uq_workspace_member UNIQUE (workspace_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_wm_user_id ON workspace_members (user_id);
CREATE INDEX IF NOT EXISTS idx_wm_workspace_id ON workspace_members (workspace_id);

-- 5. 创建文件夹表（归属于工作空间）
CREATE TABLE IF NOT EXISTS folder
(
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(255) NOT NULL,
    parent_id    INTEGER REFERENCES folder (id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    created_at   TIMESTAMP DEFAULT timezone('Asia/Shanghai', now())
);
CREATE INDEX IF NOT EXISTS idx_folder_workspace_id ON folder (workspace_id);
CREATE INDEX IF NOT EXISTS idx_folder_ws_parent ON folder (workspace_id, parent_id);

-- 6. 创建文件表（归属于工作空间，uploader_id 记录上传者）
CREATE TABLE IF NOT EXISTS files
(
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(255) NOT NULL,
    file_path    VARCHAR(512) NOT NULL,
    file_size    BIGINT,
    mime_type    VARCHAR(255),
    content_hash VARCHAR(64),                            -- 文件内容 SHA-256，用于秒传去重
    status       VARCHAR(20)   DEFAULT 'pending',        -- pending, processing, success, fail
    vector_info  vector(1024),
    description  VARCHAR(4096),
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    uploader_id  INTEGER REFERENCES users (id),          -- 记录上传者，用于归属标注和通知
    parent_id    INTEGER REFERENCES folder (id),
    created_at   TIMESTAMP DEFAULT timezone('Asia/Shanghai', now())
);

-- 创建向量索引 (HNSW, cosine distance)
CREATE INDEX IF NOT EXISTS file_vector_idx ON files USING hnsw (vector_info vector_cosine_ops);
-- 复合索引
CREATE INDEX IF NOT EXISTS idx_files_ws_parent ON files (workspace_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_files_ws_status ON files (workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_files_content_hash_size ON files (content_hash, file_size);

-- 6.1 文件内容分块表：RAG 按 chunk 检索，避免长文件只命中一段摘要
CREATE TABLE IF NOT EXISTS file_chunks
(
    id           SERIAL PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    page_number  INTEGER,
    content      TEXT NOT NULL,
    vector_info  vector(1024),
    created_at   TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()) NOT NULL,
    CONSTRAINT uq_file_chunks_file_index UNIQUE (file_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS file_chunk_vector_idx ON file_chunks USING hnsw (vector_info vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_file_chunks_ws_file ON file_chunks (workspace_id, file_id);

-- 7. 创建分享表 (注意表名为 shares，与 SQLAlchemy 模型一致)
CREATE TABLE IF NOT EXISTS shares
(
    id         SERIAL PRIMARY KEY,
    token      VARCHAR(512) UNIQUE NOT NULL,
    file_id    INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    created_at TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    expires_at TIMESTAMP
);

-- 8. 创建消息收件箱表
CREATE TABLE IF NOT EXISTS inbox
(
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title      VARCHAR(255),
    content    TEXT NOT NULL,
    is_read    BOOLEAN DEFAULT FALSE,
    type       VARCHAR(50) DEFAULT 'system',
    created_at TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    is_deleted BOOLEAN DEFAULT FALSE
);

-- 9. 创建系统字典表
CREATE TABLE IF NOT EXISTS sys_dict
(
    id         SERIAL PRIMARY KEY,
    key        VARCHAR(255) UNIQUE NOT NULL,
    value      VARCHAR(2048),
    des        VARCHAR(255),
    enable     BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT timezone('Asia/Shanghai', now())
);

-- 10. 创建文件变更事件表（用于增量整理，按工作空间追踪）
CREATE TABLE IF NOT EXISTS file_change_events
(
    id            SERIAL PRIMARY KEY,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    entity_type   VARCHAR(20)  NOT NULL, -- file / folder
    entity_id     INTEGER      NOT NULL,
    action        VARCHAR(32)  NOT NULL, -- create / move / rename / delete / update_meta
    old_parent_id INTEGER,
    new_parent_id INTEGER,
    old_name      VARCHAR(255),
    new_name      VARCHAR(255),
    payload       TEXT,
    created_at    TIMESTAMP DEFAULT timezone('Asia/Shanghai', now())
);
CREATE INDEX IF NOT EXISTS idx_file_change_events_ws_created
    ON file_change_events (workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_file_change_events_ws_id
    ON file_change_events (workspace_id, id);

-- 11. 创建整理检查点表（用于增量整理游标，按工作空间）
CREATE TABLE IF NOT EXISTS organize_checkpoints
(
    workspace_id     INTEGER PRIMARY KEY REFERENCES workspaces (id) ON DELETE CASCADE,
    last_event_id    INTEGER DEFAULT 0 NOT NULL,
    last_full_scan_at TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT timezone('Asia/Shanghai', now())
);

-- 12. 创建 MCP Token 表（每用户有且仅有一条有效记录；token_value 供复制与工作区注入）
CREATE TABLE IF NOT EXISTS mcp_tokens
(
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER      NOT NULL REFERENCES users (id),
    name          VARCHAR(80)  NOT NULL DEFAULT 'MCP Token',
    token_hash    VARCHAR(64)  UNIQUE NOT NULL,
    token_preview VARCHAR(32)  NOT NULL,
    token_value   TEXT,
    created_at    TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    expires_at    TIMESTAMP    NOT NULL,
    last_used_at  TIMESTAMP,
    revoked_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_user_id ON mcp_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_token_hash ON mcp_tokens (token_hash);

-- 12.1 OpenCode Runtime：每个用户在每个工作空间独立一个容器
CREATE TABLE IF NOT EXISTS opencode_runtimes
(
    id              SERIAL PRIMARY KEY,
    workspace_id    INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    container_id    VARCHAR(64),
    status          VARCHAR(20) NOT NULL DEFAULT 'stopped',
    error_message   TEXT,
    config_version  INTEGER NOT NULL DEFAULT 0,
    last_started_at TIMESTAMP,
    last_used_at    TIMESTAMP,
    created_at      TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    updated_at      TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    CONSTRAINT uq_opencode_runtime_workspace_user UNIQUE (workspace_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_opencode_runtime_workspace ON opencode_runtimes (workspace_id);
CREATE INDEX IF NOT EXISTS idx_opencode_runtime_user ON opencode_runtimes (user_id);

-- 12.2 Runtime Token：短期、可撤销并绑定 user + workspace + runtime
CREATE TABLE IF NOT EXISTS mcp_runtime_tokens
(
    id            SERIAL PRIMARY KEY,
    runtime_id    INTEGER NOT NULL REFERENCES opencode_runtimes (id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    jti           VARCHAR(64) UNIQUE NOT NULL,
    token_hash    VARCHAR(64) UNIQUE NOT NULL,
    token_preview VARCHAR(32) NOT NULL,
    created_at    TIMESTAMP DEFAULT timezone('Asia/Shanghai', now()),
    expires_at    TIMESTAMP NOT NULL,
    last_used_at  TIMESTAMP,
    revoked_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mcp_runtime_tokens_runtime ON mcp_runtime_tokens (runtime_id);
CREATE INDEX IF NOT EXISTS idx_mcp_runtime_tokens_user_workspace
    ON mcp_runtime_tokens (user_id, workspace_id);

-- 12.3 MCP 调用审计（不保存原始 Token 或文件正文）
CREATE TABLE IF NOT EXISTS mcp_audit_logs
(
    id                  SERIAL PRIMARY KEY,
    request_id          VARCHAR(64) NOT NULL,
    runtime_id          INTEGER REFERENCES opencode_runtimes (id) ON DELETE SET NULL,
    workspace_id        INTEGER REFERENCES workspaces (id) ON DELETE SET NULL,
    user_id             INTEGER REFERENCES users (id) ON DELETE SET NULL,
    role                VARCHAR(20),
    actor_type          VARCHAR(20) NOT NULL DEFAULT 'user',
    tool_name           VARCHAR(100) NOT NULL,
    arguments_summary   TEXT,
    entity_id           INTEGER,
    success             BOOLEAN NOT NULL DEFAULT FALSE,
    error_type          VARCHAR(100),
    duration_ms         INTEGER,
    created_at          TIMESTAMP DEFAULT timezone('Asia/Shanghai', now())
);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_workspace_created
    ON mcp_audit_logs (workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_runtime_created
    ON mcp_audit_logs (runtime_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_user_created
    ON mcp_audit_logs (user_id, created_at);

-- 13. 创建 Token 使用记录表
CREATE TABLE IF NOT EXISTS token_usage_logs
(
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    action            VARCHAR(50) NOT NULL,          -- chat / embedding / rewrite / vl_describe / organize
    model_name        VARCHAR(255),
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    query_summary     VARCHAR(200),
    extra_info        TEXT,
    created_at        TIMESTAMP DEFAULT timezone('Asia/Shanghai', now())
);
CREATE INDEX IF NOT EXISTS idx_token_usage_logs_user_id ON token_usage_logs (user_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_logs_user_action ON token_usage_logs (user_id, action);
CREATE INDEX IF NOT EXISTS idx_token_usage_logs_created_at ON token_usage_logs (user_id, created_at);

-- ==========================================
-- 初始化数据
-- ==========================================

-- 1. 初始化管理员用户 (密码为 'admin123' 的 hash 值)
INSERT INTO users (id, username, password_hash, role)
VALUES (
           1,
           'admin',
           'scrypt:32768:8:1$ULeZhKdUrZi46VVx$30e562fbf0a745b562fd7b2e037ba1e634cb13f82cd6f9d27763863136b16dc20e21802e44af40eaa5cee287c6bbd9e474d211b157b85dba3bb85bde49e4b2f1',
           'admin'
       )
ON CONFLICT (id) DO NOTHING;

-- 2. 初始化系统配置
INSERT INTO sys_dict (key, value, des)
VALUES ('site_name', 'SKYCloud', '站点名称（显示在网页标题与 Logo 旁）')
       ON CONFLICT (key) DO NOTHING;

-- 3. 初始化管理员的私人工作空间 (固定 id=1)
INSERT INTO workspaces (id, name, description, owner_id)
VALUES (1, 'admin的空间', '管理员默认工作空间', 1)
ON CONFLICT (id) DO NOTHING;

-- 4. 初始化管理员为工作空间成员 (admin 角色)
INSERT INTO workspace_members (workspace_id, user_id, role)
VALUES (1, 1, 'admin')
ON CONFLICT (workspace_id, user_id) DO NOTHING;

-- 5. 初始化根目录（固定 id=1，属于 admin 的工作空间 id=1）
INSERT INTO folder (id, name, workspace_id, parent_id)
VALUES (1, '/', 1, NULL)
ON CONFLICT (id) DO NOTHING;

-- 6. 初始化头像文件夹（固定 id=2，根目录固定 parent_id=1）
INSERT INTO folder (id, name, workspace_id, parent_id)
VALUES (2, '所有用户头像', 1, 1)
ON CONFLICT (id) DO NOTHING;

-- 7. 修正序列，避免后续自增主键冲突
SELECT setval(pg_get_serial_sequence('users', 'id'), COALESCE((SELECT MAX(id) FROM users), 1), true);
SELECT setval(pg_get_serial_sequence('workspaces', 'id'), COALESCE((SELECT MAX(id) FROM workspaces), 1), true);
SELECT setval(pg_get_serial_sequence('folder', 'id'), COALESCE((SELECT MAX(id) FROM folder), 1), true);
