import os
import platform
from contextlib import contextmanager

from dotenv import load_dotenv
from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base

load_dotenv()

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

# JWT 密钥 - 生产环境必须设置环境变量
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    import warnings

    warnings.warn(
        "SECRET_KEY environment variable is not set. "
        "Using default value for development only. "
        "This is insecure for production!",
        UserWarning,
    )
    SECRET_KEY = "sky_cloud_secret_key_dev_only"

# 数据库连接 URL
DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

# 创建 SQLAlchemy 引擎
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# 创建 session 工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 创建线程安全的 scoped_session
_scoped_session = scoped_session(SessionLocal)

# 声明式基类
Base = declarative_base()


class DatabaseManager:
    """数据库管理器，提供与 Flask-SQLAlchemy 兼容的接口"""

    def __init__(self):
        self._session = _scoped_session

    @property
    def session(self):
        return self._session

    @property
    def Model(self):
        return Base

    def create_all(self):
        """创建所有表"""
        Base.metadata.create_all(bind=engine)

    def remove_session(self):
        """移除当前线程的 session"""
        self._session.remove()

    # 提供常用 SQLAlchemy 类型的快捷访问
    Column = property(lambda self: __import__("sqlalchemy").Column)
    Integer = property(lambda self: __import__("sqlalchemy").Integer)
    String = property(lambda self: __import__("sqlalchemy").String)
    BigInteger = property(lambda self: __import__("sqlalchemy").BigInteger)
    DateTime = property(lambda self: __import__("sqlalchemy").DateTime)
    ForeignKey = property(lambda self: __import__("sqlalchemy").ForeignKey)
    Index = property(lambda self: __import__("sqlalchemy").Index)
    Text = property(lambda self: __import__("sqlalchemy").Text)

    def relationship(self, *args, **kwargs):
        from sqlalchemy.orm import relationship

        return relationship(*args, **kwargs)

    def backref(self, *args, **kwargs):
        from sqlalchemy.orm import backref

        return backref(*args, **kwargs)


# 全局数据库实例
db = DatabaseManager()

# Redis 客户端
redis_client = Redis(host=REDIS_HOST, port=int(REDIS_PORT), db=0, decode_responses=True)


def get_db():
    """FastAPI 依赖注入使用的数据库 session 生成器"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
