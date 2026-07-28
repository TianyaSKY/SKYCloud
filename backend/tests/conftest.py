"""测试全局 fixtures：环境变量、SQLite 会话、Redis Mock。

在导入任何 app 模块之前完成环境变量设置与 pgvector 兼容处理。
"""

import io
import os
import sys

# ---------------------------------------------------------------------------
# 1. 环境变量（必须在 app 包导入前设置）
# ---------------------------------------------------------------------------
os.environ["SECRET_KEY"] = "test-secret-key-for-unit-tests"
# 使用 PostgreSQL URL 让 extensions.py 的 create_engine 正常初始化（不会实际连接）
os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:5432/test_db"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "6379"
os.environ["POSTGRES_USER"] = "test"
os.environ["POSTGRES_PASSWORD"] = "test"
os.environ["POSTGRES_HOST"] = "127.0.0.1"
os.environ["POSTGRES_PORT"] = "5432"
os.environ["POSTGRES_DB"] = "test"
os.environ["DEFAULT_MODEL_PWD"] = "test-key"
os.environ["UPLOAD_FOLDER"] = "E:/PycharmProjects/SKYCloud/.test-tmp/uploads"
# RabbitMQ 快速失败：避免测试中连接重试等待（测试环境无可用 RabbitMQ）
os.environ["RABBITMQ_RECONNECT_DELAY_SECONDS"] = "0"
os.environ["RABBITMQ_CONNECTION_ATTEMPTS"] = "1"
os.environ["RABBITMQ_HOST"] = "127.0.0.1"
os.environ["RABBITMQ_PORT"] = "5672"

# 确保 backend 目录在 sys.path 中
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# ---------------------------------------------------------------------------
# 1.5 Mock 重量级第三方库（langchain / openai / docker）避免版本冲突和不必要连接
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock


class _FakeToolWrapper:
    """模拟 langchain @tool 装饰器：保留原函数并添加 .invoke() 方法。"""

    def __init__(self, func):
        self._func = func
        self.__name__ = getattr(func, "__name__", "unknown")
        self.__doc__ = getattr(func, "__doc__", None)
        self.__wrapped__ = func

    def invoke(self, args_dict):
        return self._func(**args_dict)

    def __call__(self, *args, **kwargs):
        return self._func(*args, **kwargs)


def _fake_langchain_tool(func=None, **kwargs):
    """替代 langchain.tools.tool 的 passthrough 装饰器。"""
    if func is not None:
        return _FakeToolWrapper(func)
    # 带参数调用 @tool(...)
    def decorator(f):
        return _FakeToolWrapper(f)
    return decorator


class _FakeFastMCP:
    """模拟 mcp.server.fastmcp.FastMCP：tool()/resource()/prompt() 返回 passthrough 装饰器。"""

    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        def decorator(f):
            return f
        return decorator

    def resource(self, *args, **kwargs):
        def decorator(f):
            return f
        return decorator

    def prompt(self, *args, **kwargs):
        def decorator(f):
            return f
        return decorator

    def streamable_http_app(self):
        return MagicMock()


_MOCK_MODULES = [
    "langchain",
    "langchain.tools",
    "langchain_core",
    "langchain_core.exceptions",
    "langchain_core.documents",
    "langchain_core.output_parsers",
    "langchain_core.prompts",
    "langchain_core.runnables",
    "langchain_openai",
    "langchain_openai.chat_models",
    "langchain_openai.chat_models.base",
    "langchain_openai.chat_models.azure",
    "langchain_openai.embeddings",
    "langgraph",
    "langgraph.prebuilt",
    "mcp",
    "mcp.server",
    "mcp.server.fastmcp",
    "openai",
    "docker",
    "websockets",
    "cv2",
    "fitz",
    "docx",
    "pdf2image",
]
for _mod in _MOCK_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# 配置 langchain.tools.tool 为 passthrough 装饰器
sys.modules["langchain.tools"].tool = _fake_langchain_tool
sys.modules["langchain"].tools = sys.modules["langchain.tools"]

# 配置 mcp.server.fastmcp.FastMCP 为 fake 类
sys.modules["mcp.server.fastmcp"].FastMCP = _FakeFastMCP

# ---------------------------------------------------------------------------
# 2. pgvector 兼容：让 Vector 类型在 SQLite 下可编译
# ---------------------------------------------------------------------------
from sqlalchemy.ext.compiler import compiles

try:
    from pgvector.sqlalchemy import VECTOR

    @compiles(VECTOR, "sqlite")
    def _compile_vector_sqlite(type_, compiler, **kw):
        return "TEXT"

except ImportError:
    pass

# ---------------------------------------------------------------------------
# 3. 导入 app 模块（此时环境变量已就位）
# ---------------------------------------------------------------------------
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infra.extensions import Base

# 导入所有模型以注册到 Base.metadata
from app.models import (  # noqa: F401
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
    WorkspaceMember,
)

# 确保 task_queue 模块已导入（供 patch 使用）
import app.infra.task_queue  # noqa: F401

# ---------------------------------------------------------------------------
# 4. SQLite 测试引擎
# ---------------------------------------------------------------------------
_test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(_test_engine, "connect")
def _enable_sqlite_fk(dbapi_conn, connection_record):
    """SQLite 默认不启用外键约束，测试中开启以模拟 PostgreSQL 行为。"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# 建表（跳过 PostgreSQL 专用索引）
Base.metadata.create_all(bind=_test_engine)

_TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_test_engine)


class InMemoryStorageClient:
    """测试用对象存储，隔离 MinIO 网络依赖。"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, local_path: str, object_name: str) -> None:
        with open(local_path, "rb") as source:
            self.objects[object_name] = source.read()

    def download_file(self, object_name: str, local_path: str) -> None:
        with open(local_path, "wb") as target:
            target.write(self.objects[object_name])

    def get_file_stream(self, object_name: str):
        return io.BytesIO(self.objects.get(object_name, b""))

    def close_file_stream(self, stream) -> None:
        stream.close()

    def delete_file(self, object_name: str) -> None:
        self.objects.pop(object_name, None)

    def file_exists(self, object_name: str) -> bool:
        return object_name in self.objects

    def get_file_size(self, object_name: str) -> int:
        return len(self.objects[object_name])

    def download_to_temp(self, object_name: str, suffix: str = "") -> str:
        import tempfile

        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        self.download_file(object_name, path)
        return path


# ---------------------------------------------------------------------------
# 5. Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    """每个测试独立的数据库会话，测试结束回滚并清表。"""
    connection = _test_engine.connect()
    transaction = connection.begin()
    sess = _TestSessionLocal(bind=connection)
    yield sess
    sess.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def mock_redis():
    """全局替换 redis_client 为 MagicMock，避免真实 Redis 连接。"""
    mock = MagicMock()
    # scan_iter 返回空迭代器
    mock.scan_iter.return_value = iter([])
    with patch("app.infra.extensions.redis_client", mock), \
         patch("app.infra.cache.redis_client", mock), \
         patch("app.features.folder.service.redis_client", mock):
        yield mock


@pytest.fixture(autouse=True)
def mock_task_queue():
    """全局 Mock RabbitMQ 发布函数，避免真实连接。"""
    with patch("app.infra.task_queue.publish_messages") as m_pub, \
         patch("app.infra.task_queue.publish_organize_task") as m_org, \
         patch("app.infra.task_queue.publish_file_tasks") as m_file:
        yield {"publish_messages": m_pub, "publish_organize_task": m_org, "publish_file_tasks": m_file}


@pytest.fixture(autouse=True)
def mock_object_storage():
    """所有单元测试使用内存对象存储，避免访问真实 MinIO。"""
    storage = InMemoryStorageClient()
    with patch("app.infra.storage._storage_client", storage):
        yield storage


@pytest.fixture()
def test_user(session):
    """创建标准测试用户。"""
    user = User(username="testuser", role="common")
    user.set_password("password123")
    session.add(user)
    session.flush()
    return user


@pytest.fixture()
def admin_user(session):
    """创建管理员测试用户。"""
    user = User(username="adminuser", role="admin")
    user.set_password("admin123")
    session.add(user)
    session.flush()
    return user


@pytest.fixture()
def test_workspace(session, test_user):
    """创建测试工作空间并将 test_user 设为 admin 成员。"""
    ws = Workspace(name="测试空间", owner_id=test_user.id)
    session.add(ws)
    session.flush()

    member = WorkspaceMember(workspace_id=ws.id, user_id=test_user.id, role="admin")
    session.add(member)

    root_folder = Folder(name="/", workspace_id=ws.id, parent_id=None)
    session.add(root_folder)
    session.flush()
    return ws


@pytest.fixture()
def test_folder(session, test_workspace):
    """在工作空间根目录下创建子文件夹。"""
    root = session.query(Folder).filter_by(workspace_id=test_workspace.id, parent_id=None).first()
    folder = Folder(name="文档", workspace_id=test_workspace.id, parent_id=root.id)
    session.add(folder)
    session.flush()
    return folder
