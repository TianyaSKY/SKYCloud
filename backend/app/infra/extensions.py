"""基础设施扩展: 数据库引擎/Session、Redis 客户端与全局配置."""

import os
from collections.abc import Callable, Generator, Iterator
from typing import Protocol, TypeAlias, cast

from dotenv import load_dotenv
from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, scoped_session, sessionmaker

from app.infra.secrets import required_secret

load_dotenv()

# ---------------------------------------------------------------------------
# 数据库配置
# ---------------------------------------------------------------------------
# Windows 本机开发时 "localhost" 常解析到 IPv6 ::1, 连接会长时间卡住;
# 默认改用 127.0.0.1。Docker 部署通过 DATABASE_HOST=db / DATABASE_URL 覆盖。
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_HOST = os.getenv("POSTGRES_HOST") or os.getenv("DATABASE_HOST", "127.0.0.1")
POSTGRES_PORT = os.getenv("POSTGRES_PORT") or os.getenv("DATABASE_PORT")
POSTGRES_DB = os.getenv("POSTGRES_DB")

# ---------------------------------------------------------------------------
# Redis 配置
# ---------------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
DEFAULT_MODEL_PWD = os.getenv("DEFAULT_MODEL_PWD", "")

# 分片和对象上传前的临时文件目录。Docker 默认挂载到 /data/uploads,
# 本机开发或测试可通过环境变量覆盖。
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "./data/uploads")

# No development fallback: every process requires an explicit signing secret.
SECRET_KEY = required_secret("SECRET_KEY")

# 连接串: 优先 DATABASE_URL, 否则由 POSTGRES_* 拼接
DATABASE_URL = os.getenv("DATABASE_URL") or (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@"
    f"{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

# SQLAlchemy 引擎: connect_timeout 避免 Windows/网络异常时无限卡在启动
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

SessionFactory: TypeAlias = Callable[[], Session]

# Session 工厂: 每次调用 SessionLocal() 得到独立会话
SessionLocal: sessionmaker[Session] = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# 线程作用域会话: Worker / 后台线程中使用 db.session 访问数据库
ScopedSession = scoped_session(SessionLocal)


class _DBProxy:
    """Flask-SQLAlchemy 风格代理, 兼容旧代码 db.session 调用."""

    @property
    def session(self) -> Session:
        return ScopedSession()


db = _DBProxy()

# 声明式基类(所有 ORM 模型继承)
class Base(DeclarativeBase):
    """Typed SQLAlchemy 2.0 declarative base for every mapped model."""


RedisScalar: TypeAlias = str | bytes | int | float | None
RedisValue: TypeAlias = (
    "RedisScalar | list[RedisValue] | set[RedisValue] | dict[RedisValue, RedisValue]"
)
RedisArgument: TypeAlias = str | bytes | int | float


# Redis 客户端: socket 超时, 避免 localhost/IPv6 问题导致启动挂死
class RedisPipeline(Protocol):
    """同步 pipeline 子集(redis-py 把同一 mixin 同时标注为同步与异步返回)."""

    def getbit(self, name: str, offset: int) -> "RedisPipeline":
        """Queue a bitmap read for the next execution."""
        ...

    def setbit(self, name: str, offset: int, value: int) -> "RedisPipeline":
        """Queue a bitmap update for the next execution."""
        ...

    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> "RedisPipeline":
        """Queue a hash update for the next execution."""
        ...

    def rename(self, src: str, dst: str) -> "RedisPipeline":
        """Queue a key rename for the next execution."""
        ...

    def delete(self, *names: str) -> "RedisPipeline":
        """Queue key deletions for the next execution."""
        ...

    def execute(self) -> list[RedisValue]:
        """Execute queued commands and return their synchronous results."""
        ...


class SyncRedisClient(Protocol):
    """本代码库实际使用的同步 Redis 契约(键值、哈希、脚本与扫描)."""

    def get(self, name: str) -> str | None:
        """Read a decoded string value, if the key exists."""
        ...

    def getdel(self, name: str) -> str | None:
        """Read and delete a decoded string value atomically."""
        ...

    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        """Write a string with optional expiry and absence precondition."""
        ...

    def setex(self, name: str, time: int, value: str) -> bool:
        """Write a string with an expiry in seconds."""
        ...

    def ttl(self, name: str) -> int:
        """Read the key's remaining lifetime or Redis's missing-expiry sentinel."""
        ...

    def delete(self, *names: str) -> int:
        """Delete keys and return the number removed."""
        ...

    def hget(self, name: str, key: str) -> str | None:
        """Read one decoded hash field, if it exists."""
        ...

    def hgetall(self, name: str) -> dict[str, str]:
        """Read all decoded fields in a hash."""
        ...

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisArgument,
    ) -> RedisValue:
        """Evaluate a Lua script and return its synchronous Redis result."""
        ...

    def scan(
        self,
        cursor: int = 0,
        match: str | None = None,
        count: int | None = None,
    ) -> tuple[int, list[str]]:
        """Scan one cursor page of decoded keys."""
        ...

    def scan_iter(
        self,
        match: str | None = None,
        count: int | None = None,
    ) -> Iterator[str]:
        """Iterate decoded keys through cursor-based scanning."""
        ...

    def pipeline(self, *, transaction: bool = True) -> RedisPipeline:
        """Create a synchronous command pipeline with the requested transaction."""
        ...


# redis-py 把同一 mixin 同时标注为同步与异步返回(Awaitable[X] | X); 同步类在运行时
# 一定返回 X, 这里用一次性 cast 把契约收敛到 SyncRedisClient。
redis_client = cast(
    "SyncRedisClient",
    Redis(
        host=REDIS_HOST,
        port=int(REDIS_PORT),
        db=0,
        decode_responses=True,
        socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT", "3")),
        socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
    ),
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入: yield 独立 session, 请求结束自动关闭."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_session_factory() -> SessionFactory:
    """Inject a factory; async callers construct/use/close sessions on their worker."""
    return SessionLocal
