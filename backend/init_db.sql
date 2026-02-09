-- SKYCloud 数据库初始化 SQL 文件
-- 注意：此脚本仅用于初始化空数据库，若表已存在可能会报错或跳过。
-- 建议在执行前清空数据库或确保无冲突。

-- 1. 创建 pgvector 扩展 (如果尚未存在)
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. 创建用户表
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(80) UNIQUE NOT NULL,
    password_hash VARCHAR(1024) NOT NULL,
    role VARCHAR(10) DEFAULT 'common', -- admin, common
    avatar VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. 创建文件夹表
CREATE TABLE IF NOT EXISTS folder (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    parent_id INTEGER REFERENCES folder(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. 创建文件表
CREATE TABLE IF NOT EXISTS files (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    file_path VARCHAR(512) NOT NULL,
    file_size BIGINT,
    mime_type VARCHAR(255),
    status VARCHAR(20) DEFAULT 'pending', -- pending, processing, success, fail
    vector_info vector(1536),
    description VARCHAR(4096),
    uploader_id INTEGER REFERENCES users(id),
    parent_id INTEGER REFERENCES folder(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建向量索引 (HNSW)
CREATE INDEX IF NOT EXISTS file_vector_idx ON files USING hnsw (vector_info vector_l2_ops);

-- 5. 创建分享表
CREATE TABLE IF NOT EXISTS share (
    id SERIAL PRIMARY KEY,
    token VARCHAR(36) UNIQUE NOT NULL,
    file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    creator_id INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    views INTEGER DEFAULT 0
);

-- 6. 创建消息收件箱表
CREATE TABLE IF NOT EXISTS inbox (
    id SERIAL PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    content TEXT,
    type VARCHAR(50) DEFAULT 'system',
    is_read BOOLEAN DEFAULT FALSE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 7. 创建系统字典表
CREATE TABLE IF NOT EXISTS sys_dict (
    id SERIAL PRIMARY KEY,
    key VARCHAR(255) UNIQUE NOT NULL,
    value VARCHAR(2048),
    des VARCHAR(255),
    enable BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==========================================
-- 初始化数据
-- ==========================================

-- 1. 初始化管理员用户 (密码为 'admin123' 的 hash 值，需根据实际加密方式生成，这里使用初始密钥生成的hash值)

INSERT INTO users (username, password_hash, role)
SELECT 'admin', 'scrypt:32768:8:1$ULeZhKdUrZi46VVx$30e562fbf0a745b562fd7b2e037ba1e634cb13f82cd6f9d27763863136b16dc20e21802e44af40eaa5cee287c6bbd9e474d211b157b85dba3bb85bde49e4b2f1', 'admin'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'admin');

-- 2. 初始化系统配置 (使用默认密码占位符，请替换为实际 API Key)
INSERT INTO sys_dict (key, value, des) VALUES
('site_name', 'SKYCloud', '站点名称（显示在网页标题与 Logo 旁）'),
('vl_api_url', 'https://api.siliconflow.cn/v1', 'Vision 模型 Base URL'),
('vl_api_key', 'pwd', 'Vision 模型 API 密钥'),
('vl_api_model', 'Qwen/Qwen3-VL-30B-A3B-Instruct', '多模态模型名称'),
('emb_api_url', 'https://api.siliconflow.cn/v1', 'Embedding 模型 Base URL'),
('emb_api_key', 'pwd', 'Embedding 模型 API 密钥'),
('emb_model_name', 'Qwen/Qwen3-Embedding-8B', '向量嵌入模型名称'),
('chat_api_url', 'https://api.siliconflow.cn/v1', 'Chat 模型 Base URL'),
('chat_api_key', 'pwd', 'Chat 模型 API 密钥'),
('chat_api_model', 'deepseek-ai/DeepSeek-V3.2', '对话模型名称')
ON CONFLICT (key) DO NOTHING;

-- 3. 初始化根目录 (依赖 admin 用户 ID)
-- 这一步比较复杂，因为需要获取 admin 的 ID。
-- 使用 CTE (Common Table Expressions) 来处理依赖关系

WITH admin_user AS (
    SELECT id FROM users WHERE username = 'admin' LIMIT 1
),
root_folder_insert AS (
    INSERT INTO folder (name, user_id, parent_id)
    SELECT '/', id, NULL FROM admin_user
    WHERE NOT EXISTS (SELECT 1 FROM folder WHERE name = '/' AND parent_id IS NULL)
    RETURNING id, user_id
)
-- 4. 初始化头像文件夹 (依赖根目录 ID)
INSERT INTO folder (name, user_id, parent_id)
SELECT '所有用户头像', user_id, id FROM root_folder_insert
WHERE NOT EXISTS (
    SELECT 1 FROM folder f 
    JOIN root_folder_insert r ON f.parent_id = r.id 
    WHERE f.name = '所有用户头像'
);
