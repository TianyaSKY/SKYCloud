"""Process-local assistant control signals; durable SQL state authorizes execution.

Permission notifications may arrive from worker threads and are delivered on
the Future's owning event loop. Provider abort completes before cancellation
is signalled, so the provider client remains open for its abort request.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any


class RunController:
    """In-process control plane for a currently streaming run.

    Database status is the durable source of truth.  These events/callbacks
    are only latency optimizations for cancellation and permission responses
    while the HTTP stream is alive.
    """

    def __init__(self) -> None:
        """Create an idle controller for one run."""
        self.cancel_event = asyncio.Event()
        self.abort_callback: Callable[[], Awaitable[Any]] | None = None
        self._permission_futures: dict[str, asyncio.Future[str]] = {}

    def set_abort_callback(self, callback: Callable[[], Awaitable[Any]]) -> None:
        """Register the provider abort used before local cancellation."""
        self.abort_callback = callback

    async def request_cancel(self) -> None:
        """Bound the provider abort, then signal the stream's cancel event."""
        try:
            if self.abort_callback:
                with suppress(Exception):
                    # Durable cancellation is authoritative. Provider abort
                    # remains best effort when its transport fails.
                    await asyncio.wait_for(self.abort_callback(), timeout=5)
        finally:
            # The engine observes this event and closes its provider client.
            # Signal it after the bounded abort RPC, so the client cannot be
            # torn down concurrently with that RPC.
            self.cancel_event.set()

    def wait_for_permission(self, permission_id: str) -> asyncio.Future[str]:
        """Return the future resolved by a durable permission response."""
        future = self._permission_futures.get(permission_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._permission_futures[permission_id] = future
        return future

    def resolve_permission(self, permission_id: str, response: str) -> bool:
        """Wake a same-worker stream after its response was committed."""
        future = self._permission_futures.get(permission_id)
        if future is None or future.done():
            return False

        def deliver() -> None:
            # Cancellation or another notification may win before this callback.
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
                # A disconnected stream may already have closed its loop;
                # the committed database response remains authoritative.
                return False
        return True


class RunRegistry:
    """Track live controllers by run id across API worker threads."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._controllers: dict[int, RunController] = {}

    def register(self, run_id: int) -> RunController:
        """Create and track the controller for a newly started run."""
        controller = RunController()
        self._controllers[run_id] = controller
        return controller

    def get(self, run_id: int) -> RunController | None:
        """Return the live controller, if this process still owns the run."""
        return self._controllers.get(run_id)

    def unregister(self, run_id: int) -> None:
        """Forget a run whose stream has settled."""
        self._controllers.pop(run_id, None)

    async def request_cancel(self, run_id: int) -> bool:
        """Signal the live producer for one run, if any."""
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
        """Deliver a committed permission response to the live producer."""
        controller = self.get(run_id)
        return bool(
            controller
            and controller.resolve_permission(permission_id, response),
        )


run_registry = RunRegistry()
