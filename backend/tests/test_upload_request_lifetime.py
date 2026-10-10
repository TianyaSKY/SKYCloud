"""Request form files must outlive started upload workers, even on cancellation."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.features.file import avatar_upload, router
from app.infra.upload_adapter import FastAPIUploadAdapter


@pytest.mark.parametrize('kind', ['single', 'batch', 'chunk', 'avatar'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cancelled_multipart_request_drains_worker_before_form_close(monkeypatch, tmp_path, kind, fault):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    streams, closed, consumed = [], [], []
    loop_thread = threading.get_ident()

    class Session:
        def __enter__(self):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            return self

        def __exit__(self, *args):
            assert threading.get_ident() == self.owner
            closed.append('session')

    def upload(*args):
        db = args[0]
        assert threading.get_ident() == db.owner
        adapters = [arg for arg in args if isinstance(arg, FastAPIUploadAdapter)]
        for arg in args:
            if isinstance(arg, list):
                adapters.extend(arg)
        streams.extend(adapter._upload_file.file for adapter in adapters)
        entered.set()
        try:
            assert release.wait(5)
            for index, adapter in enumerate(adapters):
                destination = tmp_path / f'upload-{index}'
                adapter.save(str(destination))
                consumed.append(destination.read_bytes())
            if fault:
                raise RuntimeError('worker-storage-error')
            return SimpleNamespace(to_dict=lambda: {})
        finally:
            finished.set()

    monkeypatch.setattr(router, 'assert_can_write_locked', lambda *args: None)
    monkeypatch.setattr(avatar_upload.account_repository, 'current_role', lambda *args: 'common')
    for name in ('create_uploaded_file', 'create_uploaded_files', 'save_multipart_chunk', 'upload_avatar_for_user'):
        monkeypatch.setattr(router.file_service, name, upload)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[router.get_session_factory] = lambda: Session
    for route in router.router.routes:
        if hasattr(route, 'dependant'):
            for dependency in route.dependant.dependencies:
                if dependency.name == 'workspace':
                    app.dependency_overrides[dependency.call] = lambda: SimpleNamespace(id=2)

    path, field = {
        'single': ('/files', 'file'),
        'batch': ('/files/batch', 'files'),
        'chunk': ('/files/multipart/chunk', 'chunk'),
        'avatar': ('/files/upload/avatar/1', 'avatar'),
    }[kind]
    count = 2 if kind == 'batch' else 1
    files = [(field, (f'{index}.txt', b'upload-body', 'text/plain')) for index in range(count)]
    data = {'upload_id': 'owned-upload', 'chunk_index': '0'} if kind == 'chunk' else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        pending = asyncio.create_task(client.post(path, files=files, data=data))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            pending.cancel('request-disconnected')
            await asyncio.sleep(.01)
            pending.cancel('repeat-disconnect')
            await asyncio.sleep(.01)
            assert not pending.done()
            assert not closed and not finished.is_set()
            assert len(streams) == count and all(not stream.closed for stream in streams)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending, 2)
            assert await asyncio.to_thread(finished.wait, 2)
    assert str(error.value) == 'request-disconnected'
    assert closed == ['session']
    assert consumed == [b'upload-body'] * count
    assert all(stream.closed for stream in streams)
