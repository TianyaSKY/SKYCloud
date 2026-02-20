-- SKYCloud 数据库初始化 SQL 文件
-- 注意：此脚本仅用于初始化空数据库，若表已存在可能会报错或跳过。
-- 建议在执行前清空数据库或确保无冲突。

-- 1. 创建 pgvector 扩展 (如果尚未存在)
CREATE
EXTENSION IF NOT EXISTS vector;

-- 2. 创建用户表
CREATE TABLE IF NOT EXISTS users
(
    id
    SERIAL
    PRIMARY
    KEY,
    username
    VARCHAR
(
    80
) UNIQUE NOT NULL,
    password_hash VARCHAR
(
    1024
) NOT NULL,
    role VARCHAR
(
    10
) DEFAULT 'common', -- admin, common
    avatar VARCHAR
(
    255
),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                             );

-- 3. 创建文件夹表
CREATE TABLE IF NOT EXISTS folder
(
    id
    SERIAL
    PRIMARY
    KEY,
    name
    VARCHAR
(
    255
) NOT NULL,
    parent_id INTEGER REFERENCES folder
(
    id
) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users
(
    id
)
  ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

-- 4. 创建文件表
CREATE TABLE IF NOT EXISTS files
(
    id
    SERIAL
    PRIMARY
    KEY,
    name
    VARCHAR
(
    255
) NOT NULL,
    file_path VARCHAR
(
    512
) NOT NULL,
    file_size BIGINT,
    mime_type VARCHAR
(
    255
),
    status VARCHAR
(
    20
) DEFAULT 'pending', -- pending, processing, success, fail
    vector_info vector
(
    1024
),
    description VARCHAR
(
    4096
),
    uploader_id INTEGER REFERENCES users
(
    id
),
    parent_id INTEGER REFERENCES folder
(
    id
),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

-- 创建向量索引 (HNSW)
CREATE INDEX IF NOT EXISTS file_vector_idx ON files USING hnsw (vector_info vector_l2_ops);

-- 5. 创建分享表
CREATE TABLE IF NOT EXISTS share
(
    id
    SERIAL
    PRIMARY
    KEY,
    token
    VARCHAR
(
    36
) UNIQUE NOT NULL,
    file_id INTEGER REFERENCES files
(
    id
) ON DELETE CASCADE,
    creator_id INTEGER REFERENCES users
(
    id
),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    views INTEGER DEFAULT 0
    );

-- 6. 创建消息收件箱表
CREATE TABLE IF NOT EXISTS inbox
(
    id
    SERIAL
    PRIMARY
    KEY,
    title
    VARCHAR
(
    255
) NOT NULL,
    content TEXT,
    type VARCHAR
(
    50
) DEFAULT 'system',
    is_read BOOLEAN DEFAULT FALSE,
    user_id INTEGER REFERENCES users
(
    id
) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

-- 7. 创建系统字典表
CREATE TABLE IF NOT EXISTS sys_dict
(
    id
    SERIAL
    PRIMARY
    KEY,
    key
    VARCHAR
(
    255
) UNIQUE NOT NULL,
    value VARCHAR
(
    2048
),
    des VARCHAR
(
    255
),
    enable BOOLEAN DEFAULT TRUE,
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
     );

-- 8. 创建文件变更事件表（用于增量整理）
CREATE TABLE IF NOT EXISTS file_change_events
(
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users (id) ON DELETE CASCADE,
    entity_type VARCHAR(20) NOT NULL, -- file / folder
    entity_id INTEGER NOT NULL,
    action VARCHAR(32) NOT NULL,      -- create / move / rename / delete / update_meta
    old_parent_id INTEGER,
    new_parent_id INTEGER,
    old_name VARCHAR(255),
    new_name VARCHAR(255),
    payload TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_file_change_events_user_created
    ON file_change_events (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_file_change_events_user_id
    ON file_change_events (user_id, id);

-- 9. 创建整理检查点表（用于增量整理游标）
CREATE TABLE IF NOT EXISTS organize_checkpoints
(
    user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    last_event_id INTEGER DEFAULT 0 NOT NULL,
    last_full_scan_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==========================================
-- 初始化数据
-- ==========================================

-- 1. 初始化管理员用户 (密码为 'admin123' 的 hash 值，需根据实际加密方式生成，这里使用初始密钥生成的hash值)

INSERT INTO users (username, password_hash, role)
SELECT 'admin',
       'scrypt:32768:8:1$ULeZhKdUrZi46VVx$30e562fbf0a745b562fd7b2e037ba1e634cb13f82cd6f9d27763863136b16dc20e21802e44af40eaa5cee287c6bbd9e474d211b157b85dba3bb85bde49e4b2f1',
       'admin' WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin');

-- 2. 初始化系统配置（模型配置请使用环境变量，不再写入 sys_dict）
INSERT INTO sys_dict (key, value, des)
VALUES ('site_name', 'SKYCloud', '站点名称（显示在网页标题与 Logo 旁）')
       ON CONFLICT (key) DO NOTHING;

-- 3. 初始化根目录 (依赖 admin 用户 ID)
-- 这一步比较复杂，因为需要获取 admin 的 ID。
-- 使用 CTE (Common Table Expressions) 来处理依赖关系

WITH admin_user AS (SELECT id
                    FROM users
                    WHERE username = 'admin'
    LIMIT 1
    )
   , root_folder_insert AS (
INSERT
INTO folder (name, user_id, parent_id)
SELECT '/', id, NULL
FROM admin_user
WHERE NOT EXISTS (SELECT 1 FROM folder WHERE name = '/'
  AND parent_id IS NULL)
    RETURNING id
    , user_id
    )
-- 4. 初始化头像文件夹 (依赖根目录 ID)
INSERT
INTO folder (name, user_id, parent_id)
SELECT '所有用户头像', user_id, id
FROM root_folder_insert
WHERE NOT EXISTS (SELECT 1
                  FROM folder f
                           JOIN root_folder_insert r ON f.parent_id = r.id
                  WHERE f.name = '所有用户头像');
