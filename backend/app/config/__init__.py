"""强类型化应用配置模型与集中校验.

按功能域划分为不可变、槽优化的冻结配置模型:
- upload: 文件上传大小、分片上限、用户/工作区容量配额与并发数
- database: 数据库与 Redis 连接池及超时参数
- worker: 后台 Worker 并发线程数
- rag: 检索增强生成与重排序参数
- runtime: OpenCode 运行时资源限制与 Token 配额
- assistant: 助手功能特性开关与租约心跳超时
- storage: 对象存储连接超时与重试配置
- mcp: MCP 协议请求大小与并发限制
"""

from app.config.assistant import AssistantConfig, get_assistant_config
from app.config.database import (
    DatabasePoolConfig,
    RedisPoolConfig,
    get_database_pool_config,
    get_redis_pool_config,
)
from app.config.mcp import McpConfig, get_mcp_config
from app.config.rag import RagConfig, get_rag_config
from app.config.runtime import (
    RuntimeLlmConfig,
    WorkspaceResourceConfig,
    get_runtime_llm_config,
    get_workspace_resource_config,
)
from app.config.storage import MinioConfig, get_minio_config
from app.config.upload import (
    UploadLimitsConfig,
    UploadQuotaConfig,
    get_upload_limits_config,
    get_upload_quota_config,
)
from app.config.worker import WorkerConfig, get_worker_config

__all__ = [
    "AssistantConfig",
    "DatabasePoolConfig",
    "McpConfig",
    "MinioConfig",
    "RagConfig",
    "RedisPoolConfig",
    "RuntimeLlmConfig",
    "UploadLimitsConfig",
    "UploadQuotaConfig",
    "WorkerConfig",
    "WorkspaceResourceConfig",
    "get_assistant_config",
    "get_database_pool_config",
    "get_mcp_config",
    "get_minio_config",
    "get_rag_config",
    "get_redis_pool_config",
    "get_runtime_llm_config",
    "get_upload_limits_config",
    "get_upload_quota_config",
    "get_worker_config",
    "get_workspace_resource_config",
]
