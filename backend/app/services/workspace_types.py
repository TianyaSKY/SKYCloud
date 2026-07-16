from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CreateWorkspaceCommand:
    """创建工作区所需的业务参数，不依赖 HTTP 请求模型。"""

    user_id: int
    name: str
