import os
import platform

from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from redis import Redis

load_dotenv()

db = SQLAlchemy()

# 数据库配置
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT")
POSTGRES_DB = os.getenv("POSTGRES_DB")

# Redis 配置
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
DEFAULT_MODEL_PWD = os.getenv("DEFAULT_MODEL_PWD", "")

# 上传路径配置
# 默认逻辑：
# 1. 优先使用环境变量 UPLOAD_FOLDER
# 2. 如果是 Windows，默认使用 D:\SKYCloudFilesUpload
# 3. 如果是 Linux (Docker)，默认使用 /data/uploads
if os.getenv("UPLOAD_FOLDER"):
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER")
else:
    if platform.system() == "Windows":
        UPLOAD_FOLDER = r"D:\SKYCloudFilesUpload"
    else:
        UPLOAD_FOLDER = "/data/uploads"

redis_client = Redis(host=REDIS_HOST, port=int(REDIS_PORT), db=0, decode_responses=True)
