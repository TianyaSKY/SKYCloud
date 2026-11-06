"""请求表单文件生命周期必须长于已启动的上传 worker (即使被取消) 测试."""

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from app.api.dependencies import get_current_user
from app.features.file import avatar_upload, router
from app.features.file.multipart_service import SaveMultipartChunkParams
from app.infra.extensions import get_session_factory
from app.infra.upload_adapter import FastAPIUploadAdapter


@pytest.mark.parametrize("kind", ["single", "batch", "chunk", "avatar"])
@pytest.mark.parametrize("fault", [False, True])
async def test_cancelled_multipart_request_drains_worker_before_form_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
    fault: bool,
) -> None:
    entered, release, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    streams: list[Any] = []
    closed: list[str] = []
    consumed: list[bytes] = []
    loop_thread = threading.get_ident()

    class Session:
        owner: int

        def __enter__(self) -> "Session":
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            return self

        def __exit__(self, *args: Any) -> None:
            assert threading.get_ident() == self.owner
            closed.append("session")

    def upload(*args: Any, outcome: str = "single") -> Any:
        db = args[0]
        assert threading.get_ident() == db.owner
        adapters: list[FastAPIUploadAdapter] = [
            arg for arg in args if isinstance(arg, FastAPIUploadAdapter)
        ]
        for arg in args:
            if isinstance(arg, list):
                for item in cast(list[object], arg):
                    if isinstance(item, FastAPIUploadAdapter):
                        adapters.append(item)
            elif isinstance(arg, SaveMultipartChunkParams):
                chunk = arg.chunk
                if isinstance(chunk, FastAPIUploadAdapter):
                    adapters.append(chunk)
        streams.extend(adapter.upload_file.file for adapter in adapters)
        entered.set()
        try:
            assert release.wait(5)
            for index, adapter in enumerate(adapters):
                destination = tmp_path / f"upload-{index}"
                adapter.save(str(destination))
                consumed.append(destination.read_bytes())
            if fault:
                raise RuntimeError("worker-storage-error")
            if outcome == "batch":
                return [SimpleNamespace(to_dict=lambda: cast(dict[str, Any], {})) for _ in adapters]
            if outcome == "avatar":
                return {"avatar_url": "/api/share/owned"}
            if outcome == "chunk":
                return {
                    "upload_id": "owned-upload",
                    "chunk_index": 0,
                    "uploaded_chunks": [0],
                }
            return SimpleNamespace(to_dict=lambda: cast(dict[str, Any], {}))
        finally:
            finished.set()

    def create_uploaded_files_mock(*args: Any) -> Any:
        return upload(*args, outcome="batch")

    def save_chunk_mock(db: Any, params: Any) -> Any:
        return upload(db, params, outcome="chunk")

    def upload_avatar_mock(*args: Any) -> Any:
        return upload(*args, outcome="avatar")

    def noop(*args: Any, **kwargs: Any) -> None:
        pass

    def return_common(*args: Any, **kwargs: Any) -> str:
        return "common"

    upload_service = SimpleNamespace(
        create_uploaded_file=upload,
        create_uploaded_files=create_uploaded_files_mock,
    )
    multipart_service = SimpleNamespace(
        save_multipart_chunk=save_chunk_mock,
    )

    monkeypatch.setattr(router, "assert_can_write_locked", noop)
    monkeypatch.setattr(router, "get_upload_service", lambda: upload_service)
    monkeypatch.setattr(
        router, "get_multipart_service", lambda: multipart_service
    )
    monkeypatch.setattr(
        avatar_upload,
        "upload_avatar_for_user",
        upload_avatar_mock,
    )
    monkeypatch.setattr(
        avatar_upload.account_repository, "current_role", return_common
    )
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: Session
    for route in router.router.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            for dependency in dependant.dependencies:
                if dependency.name == "workspace":
                    app.dependency_overrides[dependency.call] = (
                        lambda: SimpleNamespace(id=2)
                    )

    path, field = {
        "single": ("/files", "file"),
        "batch": ("/files/batch", "files"),
        "chunk": ("/files/multipart/chunk", "chunk"),
        "avatar": ("/files/upload/avatar/1", "avatar"),
    }[kind]
    count = 2 if kind == "batch" else 1
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        (field, (f"{index}.txt", b"upload-body", "text/plain"))
        for index in range(count)
    ]
    data: dict[str, str] = (
        {"upload_id": "owned-upload", "chunk_index": "0"}
        if kind == "chunk"
        else {}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(client.post(path, files=files, data=data))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            pending.cancel("request-disconnected")
            await asyncio.sleep(0.01)
            pending.cancel("repeat-disconnect")
            await asyncio.sleep(0.01)
            assert not pending.done()
            assert not closed and not finished.is_set()
            assert len(streams) == count and all(
                not stream.closed for stream in streams
            )
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending, 2)
            assert await asyncio.to_thread(finished.wait, 2)
    assert str(error.value) == "request-disconnected"
    assert closed == ["session"]
    assert consumed == [b"upload-body"] * count
    assert all(stream.closed for stream in streams)
