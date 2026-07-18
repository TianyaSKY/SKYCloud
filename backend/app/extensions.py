import os

from dotenv import load_dotenv
from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base

load_dotenv()

# 数据库配置
# 注意：Windows 本机开发时 "localhost" 常解析到 IPv6 ::1，会导致连接长时间卡住；
# 默认改用 127.0.0.1。Docker 部署通过 DATABASE_HOST=db / DATABASE_URL 覆盖。
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_HOST = os.getenv("POSTGRES_HOST") or os.getenv("DATABASE_HOST", "127.0.0.1")
POSTGRES_PORT = os.getenv("POSTGRES_PORT") or os.getenv("DATABASE_PORT")
POSTGRES_DB = os.getenv("POSTGRES_DB")

# Redis 配置
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
DEFAULT_MODEL_PWD = os.getenv("DEFAULT_MODEL_PWD", "")

# 容器内上传目录固定路径（不从环境变量读取）
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
DATABASE_URL = os.getenv("DATABASE_URL") or (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

# 创建 SQLAlchemy 引擎
# connect_timeout 避免 Windows / 网络异常时无限卡在 "Waiting for application startup"
_db_connect_timeout = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
    pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
    connect_args={"connect_timeout": _db_connect_timeout},
)

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




# 全局数据库实例
db = DatabaseManager()

# Redis 客户端（socket 超时，避免本机 localhost/IPv6 问题导致启动挂死）
redis_client = Redis(
    host=REDIS_HOST,
    port=int(REDIS_PORT),
    db=0,
    decode_responses=True,
    socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT", "3")),
    socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
)


def get_db():
    """FastAPI 依赖注入使用的数据库 session 生成器"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
