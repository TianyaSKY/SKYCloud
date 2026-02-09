import logging
import os
import time

from sqlalchemy import text

from app.extensions import db, engine, Base, UPLOAD_FOLDER, DEFAULT_MODEL_PWD

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def initialize_application():
    """初始化应用：创建数据库表、初始化数据等"""
    # 导入模型以注册到 Base.metadata
    from app.models import User, File, Folder, SysDict, Share, Inbox

    # 确保上传目录存在
    if not os.path.exists(UPLOAD_FOLDER):
        try:
            os.makedirs(UPLOAD_FOLDER)
            logger.info(f"Created upload directory: {UPLOAD_FOLDER}")
        except Exception as e:
            logger.error(f"Failed to create upload directory {UPLOAD_FOLDER}: {e}")

    # 数据库连接重试逻辑
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 尝试连接数据库
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

    # 尝试创建 vector 扩展（如果尚未存在）
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    except Exception as e:
        logger.warning(f"Warning: Could not create vector extension: {e}")

    # 自动创建所有表
    Base.metadata.create_all(bind=engine)

    # 尝试创建 HNSW 索引（如果尚未存在）
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS file_vector_idx ON files USING hnsw (vector_info vector_l2_ops)"
                )
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"Warning: Could not create vector index: {e}")
