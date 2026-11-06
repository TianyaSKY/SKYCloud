"""转换器资源清理与安全故障诊断测试."""
import concurrent.futures
import subprocess
import sys
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

from app.features.folder.organize import converter, description
from tests.test_llm_safe_diagnostics import PRIVATE, assert_safe, logs

_ = logs  # Pytest fixture import


def test_office_child_failure_discards_private_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    executable = tmp_path / 'synthetic-soffice'
    executable.write_text(f'#!{sys.executable}\nimport sys\nsys.stderr.write({PRIVATE!r})\nsys.exit(7)\n')
    executable.chmod(0o700)
    monkeypatch.setattr(converter, 'get_libreoffice_command', lambda: str(executable))
    with pytest.raises(subprocess.CalledProcessError) as error:
        converter.convert_office_to_pdf(PRIVATE, str(tmp_path))
    assert error.value.returncode == 7 and error.value.stderr is None and error.value.stdout is None
    assert_safe(logs, 'error_type=CalledProcessError')


def test_office_real_child_timeout_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    executable = tmp_path / 'synthetic-soffice'
    executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(30)\n')
    executable.chmod(0o700)
    assert converter.OFFICE_CONVERSION_TIMEOUT_SECONDS == 120
    monkeypatch.setattr(converter, 'OFFICE_CONVERSION_TIMEOUT_SECONDS', 0.1)
    monkeypatch.setattr(converter, 'get_libreoffice_command', lambda: str(executable))
    with pytest.raises(subprocess.TimeoutExpired):
        converter.convert_office_to_pdf(PRIVATE, str(tmp_path))
    assert_safe(logs, 'error_type=TimeoutExpired')


def test_pdf_render_failure_closes_open_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]]
) -> None:
    doc = MagicMock()
    doc.__len__.return_value = 1
    doc.load_page.side_effect = RuntimeError(PRIVATE)

    def _open(*a: object) -> MagicMock:
        return doc

    monkeypatch.setattr(converter.fitz, 'open', _open)
    assert converter.convert_pdf_to_images(PRIVATE, str(tmp_path)) == []
    doc.close.assert_called_once()
    assert_safe(logs, 'error_type=RuntimeError')


@pytest.mark.parametrize('mode', ['unopened', 'zero_frames', 'read_failure'])
def test_video_early_return_or_error_releases_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]], mode: str
) -> None:
    capture = Mock()
    capture.isOpened.return_value = mode != 'unopened'
    capture.get.return_value = 0 if mode == 'zero_frames' else 5
    capture.read.side_effect = RuntimeError(PRIVATE)

    def _vcap(*a: object) -> Mock:
        return capture

    monkeypatch.setattr(converter.cv2, 'VideoCapture', _vcap)
    assert converter.extract_video_frames(PRIVATE, str(tmp_path)) == []
    capture.release.assert_called_once()
    if mode == 'read_failure':
        assert_safe(logs, 'error_type=RuntimeError')


@pytest.mark.parametrize('mode', ['text', 'vision'])
def test_description_provider_failure_has_no_exception_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, Any]], mode: str
) -> None:
    from app.infra.llm import sync_client

    error = RuntimeError(PRIVATE)
    monkeypatch.setattr(sync_client, 'chat_completion', Mock(side_effect=error))

    def _text(*a: object) -> str:
        return PRIVATE

    def _urls(*a: object) -> list[str]:
        return ['private-image-data']

    monkeypatch.setattr(description, '_extract_text_content', _text)
    monkeypatch.setattr(description, '_get_visual_urls', _urls)
    filename = 'document.txt' if mode == 'text' else 'picture.png'
    with pytest.raises(RuntimeError) as caught:
        description.generate_file_description(str(tmp_path / filename), {'key': PRIVATE})
    assert caught.value is error
    assert_safe(logs, 'error_type=RuntimeError')


@pytest.mark.parametrize("completion_boundary", ["reap_completed", "wait_for_capacity"])
def test_worker_completed_failures_preserve_sibling_results_and_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    logs: list[tuple[str, Any]],
    completion_boundary: str,
) -> None:
    from app.infra import worker_runtime

    runtime = worker_runtime.WorkerRuntime(index_workers=3)
    outcomes: list[str] = []
    release_followup = Event()
    followup_started = Event()

    def fail(error_type: type[Exception]) -> None:
        raise error_type(PRIVATE)

    def succeed() -> None:
        outcomes.append("sibling_completed")

    def followup() -> None:
        followup_started.set()
        assert release_followup.wait(timeout=2)
        outcomes.append("followup_drained")

    def close_clients() -> None:
        assert outcomes == ["sibling_completed", "followup_drained"]
        outcomes.append("clients_closed")

    monkeypatch.setattr(worker_runtime, "close_sync_clients", close_clients)
    next_future: concurrent.futures.Future[None] | None = None
    try:
        failed = runtime.submit_index(fail, RuntimeError)
        other_failed = runtime.submit_index(fail, ValueError)
        successful = runtime.submit_index(succeed)
        with pytest.raises(RuntimeError):
            failed.result(timeout=2)
        with pytest.raises(ValueError):
            other_failed.result(timeout=2)
        assert successful.result(timeout=2) is None

        # 在任何公共边界运行之前, 三个 Future 均已完成.
        # 两个不同故障证明迭代不受集合顺序影响并继续执行.
        if completion_boundary == "reap_completed":
            runtime.reap_completed()
        else:
            runtime.wait_for_capacity("index")

        assert_safe(logs, "index executor (error_type=RuntimeError)")
        assert_safe(logs, "index executor (error_type=ValueError)")
        assert outcomes == ["sibling_completed"]
        assert set(runtime.available_queues()) == {
            worker_runtime.FILE_PROCESS_QUEUE,
            worker_runtime.ORGANIZE_FILE_QUEUE,
        }
        next_future = runtime.submit_index(followup)
        assert followup_started.wait(timeout=2)
    finally:
        release_followup.set()
        runtime.shutdown()

    assert next_future is not None and next_future.result(timeout=2) is None
    assert outcomes == [
        "sibling_completed",
        "followup_drained",
        "clients_closed",
    ]
