"""进程本地助手控制信号; 持久化 SQL 状态对执行进行授权.

权限通知可能来自工作线程, 并在 Future 所属的事件循环上投递.
提供商中止在发出取消信号前完成, 确保提供商客户端保持开启.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any


class RunController:
    """当前流式运行的进程内控制平面.

    数据库状态是持久化的事实来源. 这些事件与回调仅作为
    HTTP 流存活期间针对取消和权限响应的延迟优化.
    """

    def __init__(self) -> None:
        """为单次运行创建空闲控制器."""
        self.cancel_event = asyncio.Event()
        self.abort_callback: Callable[[], Awaitable[Any]] | None = None
        self._permission_futures: dict[str, asyncio.Future[str]] = {}

    def set_abort_callback(self, callback: Callable[[], Awaitable[Any]]) -> None:
        """注册在本地取消前使用的提供商中止回调."""
        self.abort_callback = callback

    async def request_cancel(self) -> None:
        """约束提供商中止并在完成后发出流取消事件信号."""
        try:
            if self.abort_callback:
                with suppress(Exception):
                    # 持久化取消具有权威性. 当传输失败时,
                    # 提供商中止保持尽力而为.
                    await asyncio.wait_for(self.abort_callback(), timeout=5)
        finally:
            # 引擎观察此事件并关闭其提供商客户端.
            # 在有界中止 RPC 后发出信号, 避免客户端被并发销毁.
            self.cancel_event.set()

    def wait_for_permission(self, permission_id: str) -> asyncio.Future[str]:
        """返回由持久化权限响应决议的 Future."""
        future = self._permission_futures.get(permission_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._permission_futures[permission_id] = future
        return future

    def resolve_permission(self, permission_id: str, response: str) -> bool:
        """在权限响应提交后唤醒同一工作线程的流."""
        future = self._permission_futures.get(permission_id)
        if future is None or future.done():
            return False

        def deliver() -> None:
            # 取消或其他通知可能会在此回调之前先行完成.
            if not future.done():
                future.set_result(response)

        loop = future.get_loop()
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            deliver()
        else:
            try:
                loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                # 断开连接的流可能已关闭其事件循环;
                # 已提交的数据库响应保持权威性.
                return False
        return True


class RunRegistry:
    """跨 API 工作线程按运行 ID 追踪存活控制器."""

    def __init__(self) -> None:
        """创建空注册表."""
        self._controllers: dict[int, RunController] = {}

    def register(self, run_id: int) -> RunController:
        """为新启动的运行创建并追踪控制器."""
        controller = RunController()
        self._controllers[run_id] = controller
        return controller

    def get(self, run_id: int) -> RunController | None:
        """若当前进程仍持有该运行, 则返回存活控制器."""
        return self._controllers.get(run_id)

    def unregister(self, run_id: int) -> None:
        """注销流已稳定结算的运行."""
        self._controllers.pop(run_id, None)

    async def request_cancel(self, run_id: int) -> bool:
        """向指定运行的存活生产者发出信号 (若存在)."""
        controller = self.get(run_id)
        if controller is None:
            return False
        await controller.request_cancel()
        return True

    def resolve_permission(
        self,
        run_id: int,
        permission_id: str,
        response: str,
    ) -> bool:
        """将已提交的权限响应投递至存活生产者."""
        controller = self.get(run_id)
        return bool(
            controller
            and controller.resolve_permission(permission_id, response),
        )


run_registry = RunRegistry()
