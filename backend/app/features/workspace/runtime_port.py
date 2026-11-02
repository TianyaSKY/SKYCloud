"""工作空间运行时生命周期操作的精简 RPC 契约接口."""

from typing import Literal, Protocol

from runtime_common.contracts import ContainerPayload, UserSnapshotRow
from runtime_common.json_types import JsonObject


class RuntimeController(Protocol):
    """通过 Controller RPC 提供给应用层代码调用的运行时操作集合."""

    def inspect(self, ref: str) -> ContainerPayload:
        """根据精确代际身份或已知引用检查单个被纳管的运行时."""
        ...

    def prepare(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
    ) -> ContainerPayload:
        """准备单个具备代际隔离围栏的候选运行时."""
        ...

    def retire_generation(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
    ) -> None:
        """在允许移除前对代际资源进行持久化退役标记."""
        ...

    def remove_legacy(self, ref: str) -> None:
        """移除单个已验证的历史遗留无代际运行时身份."""
        ...

    def user_snapshot(self, user_id: int) -> list[UserSnapshotRow]:
        """列出单个用户拥有的所有被纳管运行时代际身份."""
        ...

    def action(
        self,
        container_id: str,
        action: Literal["start", "stop", "restart", "remove"],
    ) -> None:
        """对精确指定的被纳管容器身份执行有界的单次操作."""
        ...

    def read_config(self, container_id: str) -> JsonObject:
        """读取单个被纳管运行时的规范化配置."""
        ...

    def write_config(self, container_id: str, config: JsonObject) -> None:
        """替换单个被纳管运行时的规范化配置."""
        ...
