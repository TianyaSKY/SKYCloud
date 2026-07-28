"""真实依赖测试 fixtures：使用真实 Redis、真实 SQLite 数据库会话。

本目录下的测试遵循「无 Mock」原则：
- 数据库：使用 SQLite 内存数据库（真实 SQL 会话）
- Redis：使用本地真实 Redis 实例（127.0.0.1:6379）
- RabbitMQ：测试环境无可用实例，相关发布函数会自然抛出连接异常，
  服务端代码已有 try/except 容错，测试验证真实异常处理路径。
- AI 外部 API（LLM/Embedding/Rerank）：需要网络与 API Key，
  相关测试标记 skip 并说明原因。

环境变量开关（设为 1 时启用对应测试，否则跳过）：
- TEST_AI_API=1      启用 AI API 相关测试（Embedding/LLM/Rerank）
- TEST_RABBITMQ=1    启用 RabbitMQ 相关测试
- TEST_SYMLINK=1     启用需要符号链接权限的测试（Windows 需管理员）
"""

import os
import sys
import types

import pytest
from redis import Redis

from app.infra.extensions import redis_client as _real_redis

# ---------------------------------------------------------------------------
# 环境变量开关：控制外部依赖测试是否执行
# ---------------------------------------------------------------------------

ENABLE_AI_API = os.environ.get("TEST_AI_API", "0") == "1"
ENABLE_RABBITMQ = os.environ.get("TEST_RABBITMQ", "0") == "1"
ENABLE_SYMLINK = os.environ.get("TEST_SYMLINK", "0") == "1"

# 条件跳过装饰器
skip_unless_ai_api = pytest.mark.skipif(
    not ENABLE_AI_API,
    reason="设置 TEST_AI_API=1 启用（需要有效 API Key 和网络访问）",
)
skip_unless_rabbitmq = pytest.mark.skipif(
    not ENABLE_RABBITMQ,
    reason="设置 TEST_RABBITMQ=1 启用（需要可用 RabbitMQ 实例）",
)
skip_unless_symlink = pytest.mark.skipif(
    not ENABLE_SYMLINK,
    reason="设置 TEST_SYMLINK=1 启用（需要符号链接权限）",
)

# ---------------------------------------------------------------------------
# 恢复 langchain_core.documents 为真实实现（父 conftest 将其 Mock 了）
# chat/service.py 和 chat/rerank.py 使用 Document 作为纯数据容器，
# 此处提供等价实现，无需安装完整 langchain 包。
# ---------------------------------------------------------------------------


class _RealDocument:
    """等价于 langchain_core.documents.Document 的轻量实现。"""

    def __init__(self, page_content: str = "", metadata: dict | None = None, **kwargs):
        self.page_content = page_content
        self.metadata = metadata if metadata is not None else {}

    def __repr__(self):
        return f"Document(page_content={self.page_content!r}, metadata={self.metadata!r})"

    def __eq__(self, other):
        if not isinstance(other, _RealDocument):
            return NotImplemented
        return self.page_content == other.page_content and self.metadata == other.metadata


# 替换被 Mock 的 langchain_core.documents 模块
_real_docs_module = types.ModuleType("langchain_core.documents")
_real_docs_module.Document = _RealDocument  # type: ignore[attr-defined]
sys.modules["langchain_core.documents"] = _real_docs_module


# ---------------------------------------------------------------------------
# 覆盖父级 conftest 的 autouse Mock fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_redis():
    """覆盖父级 mock_redis：使用真实 Redis 客户端。

    测试结束后清理 test:* 前缀的键，避免污染。
    """
    # 确认 Redis 可用
    _real_redis.ping()
    yield _real_redis
    # 清理测试产生的键
    try:
        keys = list(_real_redis.scan_iter(match="*test*"))
        if keys:
            _real_redis.delete(*keys)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def mock_task_queue():
    """覆盖父级 mock_task_queue：不做任何替换。

    RabbitMQ 在测试环境不可用（认证被拒绝），发布函数会自然抛出
    pika.exceptions.ProbableAuthenticationError。
    服务端代码已有 try/except 容错，测试验证真实异常处理路径。

    外部依赖说明：
    - 原因：测试环境无可用 RabbitMQ 实例（127.0.0.1:5672 认证被拒绝）
    - 影响范围：publish_file_tasks / publish_organize_task / publish_messages
    - 处理方式：让函数自然抛出，验证服务端容错逻辑
    """
    yield {}


# ---------------------------------------------------------------------------
# 复用父级 conftest 的数据库 fixtures（通过 pytest 继承自动可用）
# session, test_user, admin_user, test_workspace, test_folder
# 这些 fixture 使用真实 SQLite 数据库会话，无需 Mock。
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_redis():
    """提供真实 Redis 客户端，测试后清理所有测试键。"""
    _real_redis.ping()
    yield _real_redis
    # 清理所有可能产生的缓存键
    try:
        for pattern in ["user:*", "workspace:*", "sys_dict:*", "search:*", "organize:*"]:
            keys = list(_real_redis.scan_iter(match=pattern))
            if keys:
                _real_redis.delete(*keys)
    except Exception:
        pass
