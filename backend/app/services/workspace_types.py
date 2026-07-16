from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CreateWorkspaceCommand:
    """创建工作区所需的业务参数，不依赖 HTTP 请求模型。"""

    user_id: int
    name: str


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    """供 API 层返回的工作区视图，不暴露 ORM 实体。"""

    id: int
    user_id: int
    name: str
    container_id: str | None
    status: Literal["creating", "running", "stopped", "error"]
    error_message: str | None
    access_url: str | None
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class McpConnectionResult:
    """配置工作区 MCP 连接后的业务结果。"""

    mcp_url: str
    token_id: int
    config_path: str
