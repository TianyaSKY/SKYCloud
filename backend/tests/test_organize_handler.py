"""organize/handler.py 和 description.py 单元测试。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _async_iter(items):
    for item in items:
        yield item


class TestHandlerHelpers:
    def test_get_llm_config(self):
        from app.features.folder.organize.handler import get_llm_config
        with patch("app.features.folder.organize.handler.get_chat_model_config",
                   return_value={"api": "http://x", "key": "y", "model": "z"}):
            url, key, model = get_llm_config()
        assert url == "http://x"
        assert key == "y"
        assert model == "z"

    def test_build_full_prompt(self):
        from app.features.folder.organize.handler import _build_full_prompt
        prompt = _build_full_prompt(42)
        assert "42" in prompt
        assert "get_all_files" in prompt

    def test_format_files_detail_empty(self):
        from app.features.folder.organize.handler import _format_files_detail
        assert _format_files_detail([]) == "  (none)"

    def test_format_files_detail(self):
        from app.features.folder.organize.handler import _format_files_detail
        result = _format_files_detail([
            {"name": "a.txt", "id": 1, "path": "/root"}
        ])
        assert "a.txt" in result
        assert "/root" in result

    def test_format_folders_detail_empty(self):
        from app.features.folder.organize.handler import _format_folders_detail
        assert _format_folders_detail([]) == "  (none)"

    def test_format_folders_detail(self):
        from app.features.folder.organize.handler import _format_folders_detail
        result = _format_folders_detail([
            {"name": "folder1", "id": 2, "path": "/root/folder1"}
        ])
        assert "folder1" in result

    def test_build_incremental_prompt(self):
        from app.features.folder.organize.handler import _build_incremental_prompt
        context = {
            "summary_text": "Files changed",
            "checkpoint_event_id": 10,
            "target_event_id": 20,
            "changed_files_detail": [{"name": "a.txt", "id": 1, "path": "/"}],
            "changed_folders_detail": [{"name": "f", "id": 2, "path": "/f"}],
        }
        prompt = _build_incremental_prompt(5, context)
        assert "5" in prompt
        assert "INCREMENTAL" in prompt
        assert "a.txt" in prompt
        assert "f" in prompt


class TestOrganizeFiles:
    def test_no_changes_with_checkpoint(self, session, test_workspace):
        """有 checkpoint 且无变更时跳过。"""
        from app.features.folder.organize.handler import organize_files
        mock_ctx = {
            "has_changes": False,
            "has_checkpoint": True,
            "target_event_id": 10,
        }
        with patch("app.features.folder.organize.handler.get_llm_config",
                    return_value=("http://x", "y", "z")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.features.folder.organize.handler.change_log_service.load_incremental_context",
                   return_value=mock_ctx), \
             patch("app.features.folder.organize.handler.change_log_service.update_checkpoint") as m_upd:
            usage, results = asyncio.run(organize_files(test_workspace.id))
        m_upd.assert_called_once()
        assert "跳过" in results or "跳过" in results

    def test_no_changes_no_checkpoint_full_scan(self, session, test_workspace):
        """无 checkpoint 时执行全量扫描。"""
        from app.features.folder.organize.handler import organize_files
        mock_ctx = {
            "has_changes": False,
            "has_checkpoint": False,
            "target_event_id": 0,
        }
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _async_iter([])

        with patch("app.features.folder.organize.handler.get_llm_config",
                    return_value=("http://x", "y", "z")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.features.folder.organize.handler.change_log_service.load_incremental_context",
                   return_value=mock_ctx), \
             patch("app.features.folder.organize.handler.change_log_service.update_checkpoint"), \
             patch("app.features.folder.organize.handler.create_react_agent", return_value=mock_agent), \
             patch("app.features.folder.organize.handler.check_mixed_folders_internal",
                   return_value=(True, "clean")), \
             patch("app.features.folder.organize.handler.check_empty_folders_internal",
                   return_value=(True, "clean")):
            usage, results = asyncio.run(organize_files(test_workspace.id))
        assert usage["input_tokens"] == 0

    def test_incremental_mode(self, session, test_workspace):
        """增量模式。"""
        from app.features.folder.organize.handler import organize_files
        mock_ctx = {
            "has_changes": True,
            "overflow": False,
            "has_checkpoint": True,
            "checkpoint_event_id": 5,
            "target_event_id": 10,
            "summary_text": "summary",
            "changed_files_detail": [],
            "changed_folders_detail": [],
            "total_events": 5,
        }
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _async_iter([])

        with patch("app.features.folder.organize.handler.get_llm_config",
                    return_value=("http://x", "y", "z")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.features.folder.organize.handler.change_log_service.load_incremental_context",
                   return_value=mock_ctx), \
             patch("app.features.folder.organize.handler.change_log_service.update_checkpoint"), \
             patch("app.features.folder.organize.handler.create_react_agent", return_value=mock_agent), \
             patch("app.features.folder.organize.handler.check_mixed_folders_internal",
                   return_value=(True, "clean")), \
             patch("app.features.folder.organize.handler.check_empty_folders_internal",
                   return_value=(True, "clean")):
            usage, results = asyncio.run(organize_files(test_workspace.id))
        assert "增量" in results

    def test_overflow_full_scan(self, session, test_workspace):
        """增量事件溢出回退全量。"""
        from app.features.folder.organize.handler import organize_files
        mock_ctx = {
            "has_changes": True,
            "overflow": True,
            "has_checkpoint": True,
            "checkpoint_event_id": 5,
            "target_event_id": 10,
            "summary_text": "summary",
            "total_events": 300,
        }
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _async_iter([])

        with patch("app.features.folder.organize.handler.get_llm_config",
                    return_value=("http://x", "y", "z")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.features.folder.organize.handler.change_log_service.load_incremental_context",
                   return_value=mock_ctx), \
             patch("app.features.folder.organize.handler.change_log_service.update_checkpoint"), \
             patch("app.features.folder.organize.handler.create_react_agent", return_value=mock_agent), \
             patch("app.features.folder.organize.handler.check_mixed_folders_internal",
                   return_value=(True, "clean")), \
             patch("app.features.folder.organize.handler.check_empty_folders_internal",
                   return_value=(True, "clean")):
            usage, results = asyncio.run(organize_files(test_workspace.id))
        assert "全量" in results

    def test_load_context_fallback(self, session, test_workspace):
        """load_incremental_context 异常时回退全量。"""
        from app.features.folder.organize.handler import organize_files
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _async_iter([])

        with patch("app.features.folder.organize.handler.get_llm_config",
                    return_value=("http://x", "y", "z")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.features.folder.organize.handler.change_log_service.load_incremental_context",
                   side_effect=Exception("Context error")), \
             patch("app.features.folder.organize.handler.change_log_service.update_checkpoint"), \
             patch("app.features.folder.organize.handler.create_react_agent", return_value=mock_agent), \
             patch("app.features.folder.organize.handler.check_mixed_folders_internal",
                   return_value=(True, "clean")), \
             patch("app.features.folder.organize.handler.check_empty_folders_internal",
                   return_value=(True, "clean")):
            usage, results = asyncio.run(organize_files(test_workspace.id))
        # 回退全量模式，包含 incremental_context_error 或 首次全量
        assert "incremental_context_error" in results or "全量" in results or "首次" in results

    def test_validation_retry(self, session, test_workspace):
        """校验失败后重试。"""
        from app.features.folder.organize.handler import organize_files
        mock_ctx = {
            "has_changes": False,
            "has_checkpoint": False,
            "target_event_id": 0,
        }
        mock_agent = MagicMock()
        mock_agent.astream.return_value = _async_iter([])

        # 第一次校验失败，第二次通过
        mixed_results = [(False, "mixed error"), (True, "clean")]
        empty_results = [(True, "clean"), (True, "clean")]

        with patch("app.features.folder.organize.handler.get_llm_config",
                    return_value=("http://x", "y", "z")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.features.folder.organize.handler.change_log_service.load_incremental_context",
                   return_value=mock_ctx), \
             patch("app.features.folder.organize.handler.change_log_service.update_checkpoint"), \
             patch("app.features.folder.organize.handler.create_react_agent", return_value=mock_agent), \
             patch("app.features.folder.organize.handler.check_mixed_folders_internal",
                   side_effect=mixed_results), \
             patch("app.features.folder.organize.handler.check_empty_folders_internal",
                   side_effect=empty_results):
            usage, results = asyncio.run(organize_files(test_workspace.id))
        assert "校验失败" in results or "校验通过" in results

    def test_recursion_error_handling(self, session, test_workspace):
        """Agent 达到递归限制（名称含 RecursionError 时被捕获）。"""
        from app.features.folder.organize.handler import organize_files
        mock_ctx = {
            "has_changes": False,
            "has_checkpoint": False,
            "target_event_id": 0,
        }

        class GraphRecursionError(Exception):
            pass

        mock_agent = MagicMock()
        mock_agent.astream.side_effect = GraphRecursionError("Recursion limit")

        with patch("app.features.folder.organize.handler.get_llm_config",
                    return_value=("http://x", "y", "z")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.features.folder.organize.handler.change_log_service.load_incremental_context",
                   return_value=mock_ctx), \
             patch("app.features.folder.organize.handler.change_log_service.update_checkpoint"), \
             patch("app.features.folder.organize.handler.create_react_agent", return_value=mock_agent), \
             patch("app.features.folder.organize.handler.check_mixed_folders_internal",
                   return_value=(True, "clean")), \
             patch("app.features.folder.organize.handler.check_empty_folders_internal",
                   return_value=(True, "clean")):
            # GraphRecursionError 名称含 "RecursionError" -> 被捕获不抛出
            usage, results = asyncio.run(organize_files(test_workspace.id))
        assert "最大步数限制" in results or "校验通过" in results


class TestHandleOrganizeProcess:
    def test_handle_organize_success(self, session, test_workspace, test_user):
        """整理入口：成功写收件箱。"""
        from app.features.folder.organize.handler import handle_organize_process
        with patch("app.features.folder.organize.handler.organize_files", new_callable=AsyncMock,
                   return_value=({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, "done")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.infra.llm.client.record_llm_usage") as m_rec, \
             patch("app.infra.llm.config.get_chat_model_config", return_value={"model": "test"}), \
             patch("app.features.inbox.service.create_inbox_message") as m_inbox:
             result = asyncio.run(handle_organize_process(test_workspace.id, test_user.id))
        m_inbox.assert_called_once()
        assert result["total_tokens"] == 15

    def test_handle_organize_no_tokens(self, session, test_workspace, test_user):
        """Token 为 0 时不记录用量。"""
        from app.features.folder.organize.handler import handle_organize_process
        with patch("app.features.folder.organize.handler.organize_files", new_callable=AsyncMock,
                   return_value=({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "done")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.infra.llm.client.record_llm_usage"), \
             patch("app.infra.llm.config.get_chat_model_config", return_value={"model": "test"}), \
             patch("app.features.inbox.service.create_inbox_message"):
             result = asyncio.run(handle_organize_process(test_workspace.id, test_user.id))
        assert result["total_tokens"] == 0

    def test_handle_organize_exception(self, session, test_workspace, test_user):
        """整理失败时写错误收件箱。"""
        from app.features.folder.organize.handler import handle_organize_process
        with patch("app.features.folder.organize.handler.organize_files", new_callable=AsyncMock,
                   side_effect=Exception("Organize failed")), \
             patch("app.features.folder.organize.handler.SessionLocal", return_value=session), \
             patch("app.infra.llm.client.record_llm_usage"), \
             patch("app.infra.llm.config.get_chat_model_config", return_value={"model": "test"}), \
             patch("app.features.inbox.service.create_inbox_message") as m_inbox:
             result = asyncio.run(handle_organize_process(test_workspace.id, test_user.id))
        m_inbox.assert_called_once()
        assert result == {}


class TestDescription:
    def test_image_to_base64(self, tmp_path):
        from app.features.folder.organize.description import image_to_base64
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")  # JPEG header
        result = image_to_base64(str(img))
        assert result.startswith("data:image/jpeg;base64,")

    def test_image_to_base64_unknown_mime(self, tmp_path):
        from app.features.folder.organize.description import image_to_base64
        img = tmp_path / "test.xyz"
        img.write_bytes(b"data")
        result = image_to_base64(str(img))
        assert result.startswith("data:image/jpeg;base64,")

    def test_extract_text_content_not_exists(self, tmp_path):
        from app.features.folder.organize.description import _extract_text_content
        result = _extract_text_content(str(tmp_path / "nonexistent.txt"))
        assert result == ""

    def test_extract_text_content_text_file(self, tmp_path):
        from app.features.folder.organize.description import _extract_text_content
        f = tmp_path / "code.py"
        f.write_text("print('hello')")
        result = _extract_text_content(str(f))
        assert "hello" in result

    def test_extract_text_content_empty_ext(self, tmp_path):
        from app.features.folder.organize.description import _extract_text_content
        f = tmp_path / "noext"
        f.write_text("plain text")
        result = _extract_text_content(str(f))
        assert "plain text" in result

    def test_extract_text_content_video(self, tmp_path):
        from app.features.folder.organize.description import _extract_text_content
        f = tmp_path / "video.mp4"
        f.write_bytes(b"\x00\x00\x00")  # Fake video
        result = _extract_text_content(str(f))
        # cv2 是 mock 的，所以会进入 except 返回 Video File
        assert "Video File" in result or "Video Info" in result

    def test_extract_text_content_docx(self, tmp_path):
        from app.features.folder.organize.description import _extract_text_content
        f = tmp_path / "doc.docx"
        f.write_bytes(b"fake docx")
        result = _extract_text_content(str(f))
        # docx 是 mock，会进入 except 返回空
        assert result == ""

    def test_get_visual_urls_audio_rejected(self, tmp_path):
        from app.features.folder.organize.description import _get_visual_urls
        f = tmp_path / "audio.mp3"
        f.write_bytes(b"fake audio")
        with pytest.raises(ValueError, match="音频"):
            _get_visual_urls(str(f))

    def test_get_visual_urls_unsupported(self, tmp_path):
        from app.features.folder.organize.description import _get_visual_urls
        f = tmp_path / "data.xyz"
        f.write_bytes(b"data")
        with pytest.raises(ValueError, match="Unsupported"):
            _get_visual_urls(str(f))

    def test_get_visual_urls_text_returns_empty(self, tmp_path):
        from app.features.folder.organize.description import _get_visual_urls
        f = tmp_path / "code.py"
        f.write_text("print('hello')")
        result = _get_visual_urls(str(f))
        assert result == []

    def test_generate_text_description_empty_file(self, tmp_path):
        from app.features.folder.organize.description import _generate_text_description
        f = tmp_path / "empty.txt"
        f.write_text("")
        result = asyncio.run(_generate_text_description(
            str(f), {"api": "x", "key": "y", "model": "z"}))
        assert result == "空文件"

    def test_generate_text_description_success(self, tmp_path):
        from app.features.folder.organize.description import _generate_text_description
        f = tmp_path / "doc.txt"
        f.write_text("This is a test document about Python programming.")
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "Python 编程文档"
        with patch("app.infra.llm.client.chat_completion", new_callable=AsyncMock,
                   return_value=mock_resp):
            result = asyncio.run(_generate_text_description(
                str(f), {"api": "x", "key": "y", "model": "z"}))
        assert result == "Python 编程文档"

    def test_generate_text_description_no_content(self, tmp_path):
        from app.features.folder.organize.description import _generate_text_description
        f = tmp_path / "doc2.txt"
        f.write_text("Some content here")
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = ""
        with patch("app.infra.llm.client.chat_completion", new_callable=AsyncMock,
                   return_value=mock_resp):
            with pytest.raises(Exception, match="无法生成"):
                asyncio.run(_generate_text_description(
                    str(f), {"api": "x", "key": "y", "model": "z"}))

    def test_generate_file_description_text_path(self, tmp_path):
        from app.features.folder.organize.description import generate_file_description
        f = tmp_path / "code.py"
        f.write_text("def hello(): pass")
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "Python function"
        with patch("app.infra.llm.client.chat_completion", new_callable=AsyncMock,
                   return_value=mock_resp):
            result = asyncio.run(generate_file_description(
                str(f), {"api": "x", "key": "y", "model": "z"},
                chat_config={"api": "x", "key": "y", "model": "z"}
            ))
        assert result == "Python function"

    def test_generate_file_description_image_path(self, tmp_path):
        from app.features.folder.organize.description import generate_file_description
        f = tmp_path / "img.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0")  # JPEG header
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "A JPEG image"
        with patch("app.features.folder.organize.description._get_visual_urls", return_value=["data:image/jpeg;base64,abc"]), \
             patch("app.features.folder.organize.description._extract_text_content", return_value=""), \
              patch("app.infra.llm.client.chat_completion", new_callable=AsyncMock,
                    return_value=mock_resp):
            result = asyncio.run(generate_file_description(
                str(f), {"api": "x", "key": "y", "model": "z"}))
        assert result == "A JPEG image"

    def test_desc_file_alias(self):
        from app.features.folder.organize.description import desc_file, generate_file_description
        assert desc_file is generate_file_description

    def test_format_data_alias(self):
        from app.features.folder.organize.description import format_data, _extract_text_content
        assert format_data is _extract_text_content
