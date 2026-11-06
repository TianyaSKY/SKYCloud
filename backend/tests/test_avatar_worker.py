"""头像适配与最新角色检查保留在所属 SQL worker 上测试."""
import asyncio
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from app.api.dependencies import RequestContext
from app.features.file import avatar_upload, router


@pytest.mark.parametrize('encoded', [False, True])
async def test_avatar_adaptation_and_sql_share_owner_without_blocking_loop(
    monkeypatch: pytest.MonkeyPatch, encoded: bool
) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    sessions: list[Any] = []
    closed: list[Any] = []

    class Session:
        def __init__(self) -> None:
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            sessions.append(self)

        def __enter__(self) -> "Session":
            return self

        def __exit__(self, *args: object) -> None:
            assert threading.get_ident() == self.owner
            closed.append(self)

    def role(db: Any, actor: int) -> str:
        assert threading.get_ident() == db.owner and actor == 1
        return 'common'

    adapter = object()

    def adapt(*args: object) -> object:
        assert threading.get_ident() == sessions[0].owner
        entered.set()
        assert release.wait(5)
        return adapter

    def upload(db: Any, actor: int, role: str, target: int, value: object) -> dict[str, str]:
        assert threading.get_ident() == db.owner
        assert (actor, role, target, value) == (1, 'common', 1, adapter)
        return {'avatar_url': '/api/share/issued'}

    monkeypatch.setattr(avatar_upload.account_repository, 'current_role', role)
    monkeypatch.setattr(avatar_upload, 'Base64UploadAdapter', adapt)
    monkeypatch.setattr(avatar_upload, 'FastAPIUploadAdapter', adapt)
    monkeypatch.setattr(avatar_upload, 'upload_avatar_for_user', upload)
    pending = asyncio.create_task(
        router.upload_avatar(
            user_id=1,
            request=cast(Any, SimpleNamespace(headers={})),
            avatar=None if encoded else cast(Any, SimpleNamespace(filename='avatar.png')),
            avatar_base64='eA==' if encoded else None,
            ctx=RequestContext(
                user=cast(Any, SimpleNamespace(id=1, role='admin')),
                session_factory=cast(Any, Session),
            ),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)

        async def probe() -> bool:
            assert not release.is_set()
            return True

        assert await asyncio.wait_for(probe(), 1) and not pending.done()
    finally:
        release.set()
        result = await pending
    result_data = cast(dict[str, Any], result['data'])
    assert result_data['avatar'] == '/api/share/issued'
    assert len(sessions) == 1 and closed == sessions


def test_current_role_denial_precedes_adaptation(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext

    from app.exceptions import PermissionDeniedError

    def _mock_role(*args: object) -> str:
        return 'common'

    monkeypatch.setattr(avatar_upload.account_repository, 'current_role', _mock_role)
    adapter = Mock(side_effect=AssertionError('Denied actor must not adapt upload'))
    monkeypatch.setattr(avatar_upload, 'Base64UploadAdapter', adapter)
    with pytest.raises(PermissionDeniedError):
        avatar_upload.upload_avatar_owned(cast(Any, lambda: nullcontext(object())), 1, 2, encoded='eA==')
    adapter.assert_not_called()
