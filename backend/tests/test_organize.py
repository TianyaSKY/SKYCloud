"""文件整理模块测试：converter + description + handler 工具函数 + tools 内部函数。"""

import os
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

# ---------------------------------------------------------------------------
# converter 模块
# ---------------------------------------------------------------------------

from app.features.folder.organize.converter import (
    get_libreoffice_command,
    convert_office_to_pdf,
    convert_pdf_to_images,
    extract_video_frames,
)


class TestGetLibreofficeCommand:
    def test_env_path(self, monkeypatch, tmp_path):
        fake_path = str(tmp_path / "soffice")
        Path(fake_path).touch()
        monkeypatch.setenv("LIBREOFFICE_PATH", fake_path)
        assert get_libreoffice_command() == fake_path

    def test_env_path_not_exists(self, monkeypatch):
        monkeypatch.setenv("LIBREOFFICE_PATH", "/nonexist/soffice")
        result = get_libreoffice_command()
        assert result is not None  # 回退到 soffice 或 Windows 路径

    def test_no_env(self, monkeypatch):
        monkeypatch.delenv("LIBREOFFICE_PATH", raising=False)
        result = get_libreoffice_command()
        assert isinstance(result, str)


class TestConvertOfficeToPdf:
    def test_success(self, tmp_path):
        input_file = str(tmp_path / "test.docx")
        Path(input_file).touch()

        with patch("app.features.folder.organize.converter.subprocess.run") as mock_run, \
             patch("app.features.folder.organize.converter.get_libreoffice_command", return_value="soffice"):
            result = convert_office_to_pdf(input_file, str(tmp_path))
        assert result.endswith(".pdf")
        mock_run.assert_called_once()

    def test_command_not_found(self, tmp_path):
        input_file = str(tmp_path / "test.docx")
        Path(input_file).touch()

        with patch("app.features.folder.organize.converter.subprocess.run", side_effect=FileNotFoundError), \
             patch("app.features.folder.organize.converter.get_libreoffice_command", return_value="soffice"):
            with pytest.raises(FileNotFoundError):
                convert_office_to_pdf(input_file, str(tmp_path))


class TestConvertPdfToImages:
    def test_success(self, tmp_path):
        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 2
        mock_page = MagicMock()
        mock_pix = MagicMock()
        mock_doc.load_page.return_value = mock_page
        mock_page.get_pixmap.return_value = mock_pix

        with patch("app.features.folder.organize.converter.fitz.open", return_value=mock_doc):
            result = convert_pdf_to_images("/fake.pdf", str(tmp_path), max_pages=2)
        assert len(result) == 2

    def test_exception(self, tmp_path):
        with patch("app.features.folder.organize.converter.fitz.open", side_effect=Exception("err")):
            result = convert_pdf_to_images("/fake.pdf", str(tmp_path))
        assert result == []


class TestExtractVideoFrames:
    def test_cannot_open(self, tmp_path):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False
        with patch("app.features.folder.organize.converter.cv2.VideoCapture", return_value=mock_cap):
            result = extract_video_frames("/fake.mp4", str(tmp_path))
        assert result == []

    def test_no_frames(self, tmp_path):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.return_value = 0
        with patch("app.features.folder.organize.converter.cv2.VideoCapture", return_value=mock_cap):
            result = extract_video_frames("/fake.mp4", str(tmp_path))
        assert result == []

    def test_success(self, tmp_path):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.return_value = 100  # total frames
        mock_cap.read.return_value = (True, MagicMock())

        with patch("app.features.folder.organize.converter.cv2.VideoCapture", return_value=mock_cap), \
             patch("app.features.folder.organize.converter.cv2.imwrite"):
            result = extract_video_frames("/fake.mp4", str(tmp_path), frame_count=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# description 模块
# ---------------------------------------------------------------------------

from app.features.folder.organize.description import (
    image_to_base64,
    _get_visual_urls,
    _extract_text_content,
    _generate_text_description,
    generate_file_description,
    TEXT_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    VIDEO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    desc_file,
    format_data,
)


class TestImageToBase64:
    def test_jpeg(self, tmp_path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0fake_jpeg_data")
        result = image_to_base64(str(img))
        assert result.startswith("data:image/jpeg;base64,")

    def test_png(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG fake data")
        result = image_to_base64(str(img))
        assert result.startswith("data:image/png;base64,")


class TestGetVisualUrls:
    def test_audio_raises(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.touch()
        with pytest.raises(ValueError, match="暂不支持音频"):
            _get_visual_urls(str(f))

    def test_text_returns_empty(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        result = _get_visual_urls(str(f))
        assert result == []

    def test_image(self, tmp_path):
        f = tmp_path / "test.jpg"
        f.write_bytes(b"\xff\xd8data")
        result = _get_visual_urls(str(f))
        assert len(result) == 1
        assert result[0].startswith("data:image/jpeg")

    def test_unsupported_type(self, tmp_path):
        f = tmp_path / "test.xyz"
        f.touch()
        with pytest.raises(ValueError, match="Unsupported"):
            _get_visual_urls(str(f))


class TestExtractTextContent:
    def test_nonexist(self):
        assert _extract_text_content("/nonexist/file.txt") == ""

    def test_text_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        assert _extract_text_content(str(f)) == "hello world"

    def test_python_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x = 1", encoding="utf-8")
        assert "x = 1" in _extract_text_content(str(f))

    def test_video_file(self, tmp_path):
        f = tmp_path / "test.mp4"
        f.touch()
        mock_cap = MagicMock()
        mock_cap.get.side_effect = [1920, 1080, 30.0, 300]
        with patch("app.features.folder.organize.description.cv2.VideoCapture", return_value=mock_cap):
            result = _extract_text_content(str(f))
        assert "Video Info" in result

    def test_unknown_ext(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"\x00\x01")
        result = _extract_text_content(str(f))
        assert result == ""


class TestGenerateTextDescription:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = _generate_text_description(str(f), {"api": "a", "key": "k", "model": "m"})
        assert result == "空文件"

    def test_success(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("some content here", encoding="utf-8")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "这是一个测试文件"

        with patch("app.infra.llm.client.chat_completion", return_value=mock_resp):
            result = _generate_text_description(str(f), {"api": "a", "key": "k", "model": "m"})
        assert result == "这是一个测试文件"


class TestGenerateFileDescription:
    def test_text_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x = 1", encoding="utf-8")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "Python代码文件"

        with patch("app.infra.llm.client.chat_completion", return_value=mock_resp):
            result = generate_file_description(str(f), {"api": "a", "key": "k", "model": "m"})
        assert result == "Python代码文件"

    def test_compat_aliases(self):
        assert desc_file is generate_file_description
        assert format_data is _extract_text_content


# ---------------------------------------------------------------------------
# handler 工具函数
# ---------------------------------------------------------------------------

from app.features.folder.organize.handler import (
    get_llm_config,
    _build_full_prompt,
    _format_files_detail,
    _format_folders_detail,
    _build_incremental_prompt,
    handle_organize_process,
)


class TestGetLlmConfig:
    def test_returns_tuple(self):
        with patch("app.features.folder.organize.handler.get_chat_model_config",
                   return_value={"api": "http://a", "key": "k", "model": "m"}):
            api, key, model = get_llm_config()
        assert api == "http://a"
        assert key == "k"
        assert model == "m"


class TestBuildFullPrompt:
    def test_contains_workspace_id(self):
        prompt = _build_full_prompt(42)
        assert "42" in prompt
        assert "Single Content Principle" in prompt


class TestFormatFilesDetail:
    def test_empty(self):
        assert _format_files_detail([]) == "  (none)"

    def test_with_items(self):
        items = [{"name": "a.txt", "id": 1, "path": "根目录/docs"}]
        result = _format_files_detail(items)
        assert "a.txt" in result
        assert "ID: 1" in result


class TestFormatFoldersDetail:
    def test_empty(self):
        assert _format_folders_detail([]) == "  (none)"

    def test_with_items(self):
        items = [{"name": "docs", "id": 5, "path": "根目录/docs"}]
        result = _format_folders_detail(items)
        assert "docs" in result


class TestBuildIncrementalPrompt:
    def test_basic(self):
        context = {
            "summary_text": "test summary",
            "checkpoint_event_id": 0,
            "target_event_id": 10,
            "changed_files_detail": [{"name": "f.txt", "id": 1, "path": "根目录"}],
            "changed_folders_detail": [],
        }
        prompt = _build_incremental_prompt(1, context)
        assert "INCREMENTAL" in prompt
        assert "f.txt" in prompt


class TestHandleOrganizeProcess:
    def test_success(self):
        with patch("app.features.folder.organize.handler.organize_files",
                   return_value=({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, "done")), \
             patch("app.features.folder.organize.handler.SessionLocal") as mock_sl, \
             patch("app.infra.llm.client.record_llm_usage"), \
             patch("app.infra.llm.config.get_chat_model_config", return_value={"model": "m"}):
            mock_session = MagicMock()
            mock_sl.return_value = mock_session
            with patch("app.features.inbox.service.create_inbox_message"):
                result = handle_organize_process(1, 1)
        assert result["total_tokens"] == 15

    def test_exception(self):
        with patch("app.features.folder.organize.handler.organize_files", side_effect=Exception("fail")), \
             patch("app.features.folder.organize.handler.SessionLocal") as mock_sl:
            mock_session = MagicMock()
            mock_sl.return_value = mock_session
            with patch("app.features.inbox.service.create_inbox_message"):
                result = handle_organize_process(1, 1)
        assert result == {}


# ---------------------------------------------------------------------------
# tools 内部函数
# ---------------------------------------------------------------------------

from app.features.folder.organize.tools import (
    _check_mixed_folders,
    _check_empty_folders,
    check_mixed_folders_internal,
    check_empty_folders_internal,
    clear_workspace_cache,
)


class TestCheckMixedFolders:
    def test_clean(self, session, test_workspace, test_user):
        # 只有根文件夹，无文件无子文件夹
        is_clean, mixed = _check_mixed_folders(session, test_workspace.id)
        assert is_clean is True
        assert mixed == []

    def test_mixed_root(self, session, test_workspace, test_user):
        from app.models.folder import Folder
        from app.models.file import File
        # 根目录有子文件夹
        root = session.query(Folder).filter_by(workspace_id=test_workspace.id, parent_id=None).first()
        # 根目录也有文件 (parent_id=None)
        session.add(File(name="root_file.txt", file_path="rf.txt", file_size=10,
                         workspace_id=test_workspace.id, parent_id=None))
        session.flush()

        is_clean, mixed = _check_mixed_folders(session, test_workspace.id)
        assert is_clean is False
        assert any(f["id"] == 0 for f in mixed)


class TestCheckEmptyFolders:
    def test_no_empty(self, session, test_workspace, test_user, test_folder):
        from app.models.file import File
        # 给 test_folder 添加文件
        session.add(File(name="f.txt", file_path="f.txt", file_size=10,
                         workspace_id=test_workspace.id, parent_id=test_folder.id))
        session.flush()

        is_clean, empty = _check_empty_folders(session, test_workspace.id)
        # test_folder 不为空
        assert not any(f["id"] == test_folder.id for f in empty)

    def test_has_empty(self, session, test_workspace, test_user, test_folder):
        # test_folder 无文件无子文件夹
        is_clean, empty = _check_empty_folders(session, test_workspace.id)
        assert is_clean is False
        assert any(f["id"] == test_folder.id for f in empty)


class TestCheckInternalFunctions:
    def test_mixed_internal(self):
        mock_session = MagicMock()
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session), \
             patch("app.features.folder.organize.tools._check_mixed_folders", return_value=(True, [])):
            is_clean, msg = check_mixed_folders_internal(1)
        assert is_clean is True
        mock_session.close.assert_called_once()

    def test_empty_internal(self):
        mock_session = MagicMock()
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session), \
             patch("app.features.folder.organize.tools._check_empty_folders", return_value=(True, [])):
            is_clean, msg = check_empty_folders_internal(1)
        assert is_clean is True
        mock_session.close.assert_called_once()


class TestClearWorkspaceCache:
    def test_success(self):
        mock_redis = MagicMock()
        mock_redis.scan.return_value = (0, [])
        with patch("app.features.folder.organize.tools.redis_client", mock_redis):
            clear_workspace_cache(1)
        mock_redis.delete.assert_called()

    def test_exception(self):
        mock_redis = MagicMock()
        mock_redis.delete.side_effect = Exception("err")
        with patch("app.features.folder.organize.tools.redis_client", mock_redis):
            clear_workspace_cache(1)  # 不应抛异常
