"""应用包初始化：启动时建表、轻量 schema 对齐与向量索引校验。"""

import logging
import os
import time

from sqlalchemy import text

from app.extensions import engine, Base, UPLOAD_FOLDER, DEFAULT_MODEL_PWD

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _ensure_file_vector_index() -> None:
    """保证 files 向量索引统一为余弦距离算子（vector_cosine_ops）。"""
    expected_sql = "CREATE INDEX file_vector_idx ON files USING hnsw (vector_info vector_cosine_ops)"
    try:
        with engine.connect() as conn:
            existing_index_sql = conn.execute(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = 'files'
                      AND indexname = 'file_vector_idx'
                    """
                )
            ).scalar()

            if existing_index_sql and "vector_cosine_ops" in existing_index_sql:
                return

            if existing_index_sql:
                logger.info(
                    "Recreating file_vector_idx to use vector_cosine_ops instead of the old operator class."
                )
                conn.execute(text("DROP INDEX IF EXISTS file_vector_idx"))

            conn.execute(text(expected_sql))
            conn.commit()
    except Exception as e:
        logger.warning(f"Warning: Could not ensure vector index operator class: {e}")


def _ensure_file_content_hash_column() -> None:
    """保证 files 表存在 content_hash 列（秒传去重）及配套索引。"""
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'files'
                      AND column_name = 'content_hash'
                    """
                )
            ).scalar()
            if not exists:
                logger.info(
                    "Adding files.content_hash column for instant upload support."
                )
                conn.execute(
                    text("ALTER TABLE files ADD COLUMN content_hash VARCHAR(64)")
                )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_files_content_hash_size ON files (content_hash, file_size)"
                )
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"Warning: Could not ensure files.content_hash column: {e}")


def _ensure_mcp_token_value_column() -> None:
    """保证 mcp_tokens.token_value 存在，便于前端复制与工作区注入。"""
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'mcp_tokens'
                      AND column_name = 'token_value'
                    """
                )
            ).scalar()
            if not exists:
                logger.info("Adding mcp_tokens.token_value column for MCP token reuse.")
                conn.execute(text("ALTER TABLE mcp_tokens ADD COLUMN token_value TEXT"))
            conn.commit()
    except Exception as e:
        logger.warning(f"Warning: Could not ensure mcp_tokens.token_value column: {e}")


def initialize_application():
    """初始化应用：建上传目录、连通数据库、建表并对齐必要列/索引。"""
    # 导入模型以注册到 Base.metadata
    from app.models import (
        User,
        File,
        Folder,
        SysDict,
        Share,
        Inbox,
        McpToken,
        FileChangeEvent,
        OrganizeCheckpoint,
        TokenUsageLog,
        Workspace,
    )

    # 确保上传目录存在
    if not os.path.exists(UPLOAD_FOLDER):
        try:
            os.makedirs(UPLOAD_FOLDER)
            logger.info(f"Created upload directory: {UPLOAD_FOLDER}")
        except Exception as e:
            logger.error(f"Failed to create upload directory {UPLOAD_FOLDER}: {e}")

    # 数据库连接重试
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Database connection successful.")
            break
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(
                    f"Database connection failed, retrying in 5 seconds... (Attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(5)
            else:
                logger.error(
                    f"Failed to connect to database after {max_retries} attempts."
                )
                raise e

    # 尝试创建 pgvector 扩展（若不存在）
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    except Exception as e:
        logger.warning(f"Warning: Could not create vector extension: {e}")

    # 自动创建所有表，并做轻量 schema 对齐
    Base.metadata.create_all(bind=engine)
    _ensure_file_content_hash_column()
    _ensure_mcp_token_value_column()

    # 向量索引与检索距离度量保持一致
    _ensure_file_vector_index()
