"""运行时配置具有独立的 worker 持有数据库会话测试."""
import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import contextmanager
from dataclasses import fields
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from app.features.assistant.engines import expert_engine as module


def _noop(*args: object, **kwargs: object) -> None:
    pass


def _ret_true(*args: object, **kwargs: object) -> bool:
    return True


@pytest.mark.parametrize('needs_start', [False, True])
async def test_runtime_setup_uses_independent_sessions_and_keeps_loop_responsive(monkeypatch: pytest.MonkeyPatch, needs_start: bool) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    sessions: list[Any] = []
    closed: list[Any] = []
    runtime = SimpleNamespace(id=9, status='running', container_id='abc', error_message=None)
    workspace = SimpleNamespace(id=2)
    @contextmanager
    def factory() -> Generator[Any, None, None]:
        thread = threading.get_ident()
        assert thread != loop_thread
        def check_thread(*args: Any) -> None:
            assert threading.get_ident() == thread
        def get_ws(*args: object) -> Any:
            check_thread()
            return workspace
        session = SimpleNamespace(get=get_ws, commit=check_thread)
        sessions.append(session)
        try:
            yield session
        finally:
            check_thread()
            closed.append(session)
    stream_session = Mock()
    engine = module.ExpertEngine(
        session=cast(Any, stream_session),
        conversation=cast(Any, SimpleNamespace(id=3)),
        run=cast(Any, SimpleNamespace(id=4)),
    )
    engine.runtime_session_factory = cast(Any, factory)

    def check_can_write(session: object, *args: object) -> int:
        return sessions.index(session)

    def ret_runtime(*args: object, **kwargs: object) -> Any:
        return runtime

    def check_uses_image(*args: object) -> bool:
        return not needs_start

    def ret_base_url(*args: object) -> str:
        return 'http://controller/runtime'

    monkeypatch.setattr(module, 'assert_can_write', check_can_write)
    monkeypatch.setattr(module.runtime_service, 'get_or_create_runtime', ret_runtime)
    monkeypatch.setattr(module.runtime_service, 'get_runtime', ret_runtime)
    monkeypatch.setattr(module.docker_service, 'runtime_uses_configured_image', check_uses_image)
    calls: list[str] = []
    def block(operation: str) -> Callable[..., Any]:
        def call(session: Any, *args: Any, **kwargs: Any) -> None:
            assert threading.get_ident() != loop_thread
            assert session in sessions
            calls.append(operation)
            entered.set()
            assert release.wait(5)
        return call
    monkeypatch.setattr(module.docker_service, 'start', block('start'))
    monkeypatch.setattr(module.docker_service, 'setup_mcp', block('mcp'))
    monkeypatch.setattr(module.docker_service, 'get_runtime_base_url', ret_base_url)
    events = engine._ensure_runtime(2, 1)  # pyright: ignore[reportPrivateUsage]
    first_connection, first_event = await anext(events)
    assert first_connection is None
    assert first_event.payload['runtime_status'] == ('starting' if needs_start else 'running')
    pending = asyncio.create_task(anext(events))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # 当 Controller 操作阻塞 worker 时, 此协程仍可运行.
        async def probe() -> bool:
            assert not release.is_set()
            return True
        assert await asyncio.wait_for(probe(), 1)
        assert not pending.done()
    finally:
        release.set()
        connection, status = await pending
        await events.aclose()
    assert connection is not None
    assert connection == module.RuntimeConnection(9, 'http://controller/runtime', 'abc')
    assert [field.name for field in fields(connection)] == ['runtime_id', 'base_url', 'container_id']
    assert status.payload['runtime_status'] == 'running'
    assert calls == ['start' if needs_start else 'mcp']
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert closed == sessions
    stream_session.get.assert_not_called()
    stream_session.commit.assert_not_called()


async def test_expert_control_transactions_use_worker_sessions_and_preserve_terminal_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.exceptions import ConflictError
    loop_thread = threading.get_ident()
    run = SimpleNamespace(
        id=4,
        conversation_id=3,
        engine="expert",
        status="running",
        cancel_requested=False,
        metadata_dict={},
        opencode_session_id=None,
    )
    permission = SimpleNamespace(
        run_id=4,
        permission_id="p1",
        state="pending",
        response=None,
    )
    conversation = SimpleNamespace(
        id=3,
        title="Expert",
        opencode_session_id=None,
        opencode_runtime_id=None,
        source_conversation_id=8,
    )
    sessions: list[Any] = []
    closed: list[Any] = []

    @contextmanager
    def factory() -> Generator[Any, None, None]:
        owner = threading.get_ident()
        assert owner != loop_thread

        def check_thread(*args: Any) -> None:
            assert threading.get_ident() == owner

        def get_conv(*args: object) -> Any:
            check_thread()
            return conversation

        def refresh_session(*args: object) -> None:
            check_thread()

        session = SimpleNamespace(
            get=get_conv,
            commit=check_thread,
            refresh=refresh_session,
        )
        sessions.append(session)
        try:
            yield session
        finally:
            check_thread()
            closed.append(session)

    def register_permission(session: Any, _run: Any, payload: dict[str, Any]) -> Any:
        assert threading.get_ident() != loop_thread
        if run.status == "failed":
            raise ConflictError("run is terminal")
        permission.permission_id = payload["permission_id"]
        permission.state = "pending"
        permission.response = None
        run.status = "waiting_permission"
        return permission

    def permission_for_run(_session: Any, _run_id: Any, permission_id: str) -> Any:
        assert threading.get_ident() != loop_thread
        return permission if permission.permission_id == permission_id else None

    stream_session = Mock()
    engine = module.ExpertEngine(
        session=cast(Any, stream_session),
        conversation=cast(Any, conversation),
        run=cast(Any, run),
    )
    engine.runtime_session_factory = cast(Any, factory)

    def ret_run(*args: object) -> Any:
        return run

    monkeypatch.setattr(module.repository, "get_owned_run", ret_run)
    monkeypatch.setattr(module.repository, "lock_run", ret_run)
    monkeypatch.setattr(module.repository, "register_permission", register_permission)
    monkeypatch.setattr(module.repository, "permission_for_run", permission_for_run)
    monkeypatch.setattr(module, "assert_can_write", _noop)
    context = await asyncio.to_thread(engine._load_context, 2, 1)  # pyright: ignore[reportPrivateUsage]
    assert context == module.ExpertContext("Expert", None, 8)
    await asyncio.to_thread(engine._save_provider_session, 2, 1, 9, "s1")  # pyright: ignore[reportPrivateUsage]
    assert conversation.opencode_session_id == run.opencode_session_id == "s1"
    assert conversation.opencode_runtime_id == 9
    await asyncio.to_thread(
        engine._persist_permission,  # pyright: ignore[reportPrivateUsage]
        2,
        1,
        {"permission_id": "p1", "title": "Execute"},
    )
    assert run.status == "waiting_permission"
    assert await asyncio.to_thread(engine._permission_state, 2, 1, "p1") == (  # pyright: ignore[reportPrivateUsage]
        None,
        False,
    )
    permission.state = "answered"
    permission.response = "once"
    run.status = "running"
    assert await asyncio.to_thread(engine._permission_state, 2, 1, "other") == (  # pyright: ignore[reportPrivateUsage]
        None,
        False,
    )
    assert await asyncio.to_thread(engine._permission_state, 2, 1, "p1") == (  # pyright: ignore[reportPrivateUsage]
        "once",
        False,
    )
    run.status = "failed"
    permission.state = "cancelled"
    assert await asyncio.to_thread(engine._permission_state, 2, 1, "p2") == (  # pyright: ignore[reportPrivateUsage]
        None,
        True,
    )
    with pytest.raises(ConflictError):
        await asyncio.to_thread(
            engine._persist_permission,  # pyright: ignore[reportPrivateUsage]
            2,
            1,
            {"permission_id": "p2"},
        )
    assert run.status == "failed" and permission.permission_id == "p1"
    assert len(sessions) == 8 and closed == sessions
    stream_session.get.assert_not_called()
    stream_session.commit.assert_not_called()


@pytest.mark.parametrize('stage', ['admission', 'prepare'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cancelled_runtime_setup_drains_worker_session(monkeypatch: pytest.MonkeyPatch, stage: str, fault: bool) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    closed: list[str] = []
    runtime = SimpleNamespace(id=9, status='running', container_id='abc')
    workspace = SimpleNamespace(id=2)
    def get_workspace(*args: object) -> Any:
        return workspace
    @contextmanager
    def factory() -> Generator[Any, None, None]:
        try:
            yield SimpleNamespace(get=get_workspace, commit=_noop)
        finally:
            closed.append('closed')
    def hold(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        try:
            assert release.wait(3)
            if fault:
                raise RuntimeError('private-worker-error')
            return runtime
        finally:
            finished.set()
    engine = module.ExpertEngine(
        session=cast(Any, Mock()),
        conversation=cast(Any, SimpleNamespace(id=3)),
        run=cast(Any, SimpleNamespace(id=4)),
    )
    engine.runtime_session_factory = cast(Any, factory)

    def ret_runtime(*args: object) -> Any:
        return runtime

    def ret_runtime_url(*args: object) -> str:
        return 'http://runtime'

    monkeypatch.setattr(module, 'assert_can_write', _noop)
    monkeypatch.setattr(module.runtime_service, 'get_or_create_runtime', hold if stage == 'admission' else ret_runtime)
    monkeypatch.setattr(module.runtime_service, 'get_runtime', ret_runtime)
    monkeypatch.setattr(module.docker_service, 'runtime_uses_configured_image', _ret_true)
    monkeypatch.setattr(module.docker_service, 'setup_mcp', hold if stage == 'prepare' else _noop)
    monkeypatch.setattr(module.docker_service, 'get_runtime_base_url', ret_runtime_url)
    events = engine._ensure_runtime(2, 1)  # pyright: ignore[reportPrivateUsage]
    if stage == 'prepare':
        await anext(events)
    previous = len(closed)
    pending = asyncio.create_task(anext(events))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel('caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and len(closed) == previous and not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 1)
        assert await asyncio.to_thread(finished.wait, 1)
        await events.aclose()
    assert str(error.value) == 'caller-stop' and len(closed) == previous + 1


@pytest.mark.parametrize('stage', ['persist', 'state', 'dispatch'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cancelled_permission_worker_finishes_before_client_and_lease_close(monkeypatch: pytest.MonkeyPatch, stage: str, fault: bool) -> None:
    from app.features.assistant.run_control import RunController
    from app.features.assistant.engines.base import EngineRequest
    from app.features.assistant.event_protocol import AssistantEvent
    from contextlib import asynccontextmanager
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []
    def hold(*args: Any) -> tuple[str, bool]:
        entered.set()
        try:
            assert release.wait(3)
            if fault:
                raise RuntimeError('private-worker-error')
            return ('once', False)
        finally:
            order.append('worker-finished')

    permission = SimpleNamespace(
        state="pending",
        response=None,
        details={"permission_id": "p", "title": "Execute"},
    )

    def persist_permission(*args: Any) -> Any:
        hold(*args)
        return permission

    engine = module.ExpertEngine(
        session=cast(Any, Mock()),
        conversation=cast(Any, SimpleNamespace(id=3)),
        run=cast(Any, SimpleNamespace(id=4)),
    )
    async def ready(*args: Any) -> AsyncGenerator[tuple[Any, Any], None]:
        yield module.RuntimeConnection(9, 'http://runtime', 'abc'), AssistantEvent('status', {})
    @asynccontextmanager
    async def lease(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            yield 'token'
        finally:
            order.append('lease-close')
    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None: pass
        async def __aenter__(self) -> 'Client': return self
        async def __aexit__(self, *args: Any) -> None: order.append('client-close')
        async def health(self) -> dict[str, bool]: return {'healthy': True}
        async def get_mcp_status(self) -> dict[str, Any]: return {}
        async def get_session(self, *args: Any) -> dict[str, str]: return {'id': 'session'}
        async def get_messages(self, *args: Any) -> list[Any]: return []
        async def prompt_async(self, *args: Any, **kwargs: Any) -> None: pass
        async def abort_session(self, *args: Any) -> bool: order.append('abort'); return True
        async def stream_events(self) -> AsyncGenerator[str, None]:
            yield 'connected'
            yield 'permission'

    def map_event(event: object, *args: object) -> list[AssistantEvent]:
        return [AssistantEvent('permission_required', {'permission_id': 'p'})] if event == 'permission' else []

    def ret_context(*args: object) -> module.ExpertContext:
        return module.ExpertContext('title', 'session', None)

    def ret_permission(*args: object) -> Any:
        return permission

    def ret_permission_state(*args: object) -> tuple[str, bool]:
        return ('once', False)

    monkeypatch.setattr(engine, '_ensure_runtime', ready)
    monkeypatch.setattr(module, '_runtime_lease', lease)
    monkeypatch.setattr(module, 'OpenCodeClient', Client)
    monkeypatch.setattr(module, 'map_opencode_event', map_event)
    monkeypatch.setenv('ASSISTANT_PERMISSION_UI_ENABLED', '1')
    monkeypatch.setattr(engine, '_load_context', ret_context)
    monkeypatch.setattr(engine, '_save_provider_session', _noop)
    monkeypatch.setattr(
        engine,
        "_persist_permission",
        persist_permission if stage == "persist" else ret_permission,
    )
    monkeypatch.setattr(engine, '_permission_state', hold if stage == 'state' else ret_permission_state)
    monkeypatch.setattr(engine, '_dispatch_permission', hold if stage == 'dispatch' else _noop)
    async def consume() -> None:
        request = EngineRequest(
            run_id=4,
            user_id=1,
            workspace_id=2,
            query='query',
            history=[],
            controller=RunController(),
        )
        async for _ in engine.stream(request): pass
    pending = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel('caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and not order
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 2)
    assert str(error.value) == 'caller-stop'
    assert order.index('worker-finished') < order.index('client-close')
    assert order.index('worker-finished') < order.index('lease-close')


@pytest.mark.parametrize(
    ("reject_result", "cancelled", "expected_events"),
    [
        ("reject", False, ["status"]),
        (None, True, ["cancelled"]),
        (None, False, []),
    ],
)
async def test_disabled_permission_ui_rejects_or_stops_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    reject_result: str | None,
    cancelled: bool,
    expected_events: list[str],
) -> None:
    engine = module.ExpertEngine(
        session=cast(Any, Mock()),
        conversation=cast(Any, SimpleNamespace(id=3)),
        run=cast(Any, SimpleNamespace(id=4)),
    )
    permission = SimpleNamespace(
        state="pending",
        response=None,
        details={"permission_id": "p", "title": "Execute"},
    )
    dispatched: list[Any] = []

    async def run_inline(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    def ret_permission(*args: object) -> Any:
        return permission

    def ret_reject(*args: object) -> str | None:
        return reject_result

    def ret_state(*args: object) -> tuple[None, bool]:
        return (None, cancelled)

    def dispatch(*args: Any) -> None:
        dispatched.append(args)

    monkeypatch.setattr(module, "run_owned", run_inline)
    monkeypatch.setattr(module, "_permission_ui_enabled", lambda: False)
    monkeypatch.setattr(engine, "_persist_permission", ret_permission)
    monkeypatch.setattr(
        engine,
        "_reject_pending_permission",
        ret_reject,
    )
    monkeypatch.setattr(
        engine,
        "_permission_state",
        ret_state,
    )
    monkeypatch.setattr(
        engine,
        "_dispatch_permission",
        dispatch,
    )
    exchange = SimpleNamespace(
        inputs=SimpleNamespace(workspace_id=2, user_id=1),
        provider=SimpleNamespace(session_id="session"),
        client=SimpleNamespace(),
        runtime=SimpleNamespace(),
    )
    progress = module.StreamProgress()
    mapped = module.AssistantEvent(
        "permission_required",
        {"permission_id": "p"},
    )

    events = [
        event
        async for event in engine._resolve_permission(cast(Any, exchange), progress, mapped)  # pyright: ignore[reportPrivateUsage]
    ]

    assert [event.type for event in events] == expected_events
    assert len(dispatched) == (1 if reject_result == "reject" else 0)
    assert progress.cancelled is cancelled
    if reject_result == "reject":
        assert dispatched[0][-1].response == "reject"
        assert events[0].payload["content"] == (
            "权限确认功能未开启,已拒绝高风险操作"
        )
