"""file/service.py 完整测试。"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest


class TestFileServiceClone:
    def test_clone_existing_file_success(self, session, test_workspace):
        from app.features.file.service import _clone_existing_file
        from app.models.file import File

        source = File(
            name="a.txt", file_path="hashfile.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=1, parent_id=None,
            content_hash="a" * 64, status="success",
            description="desc", vector_info=None,
        )
        session.add(source)
        session.commit()

        with patch("app.features.file.service._clear_search_cache"):
            new_file = _clone_existing_file(
                session, source,
                filename="b.txt", workspace_id=test_workspace.id,
                uploader_id=1, parent_id=None,
                mime_type="text/plain", content_hash="a" * 64,
            )
        assert new_file.name == "b.txt"
        assert new_file.status == "success"
        assert new_file.description == "desc"

    def test_clone_existing_file_pending(self, session, test_workspace):
        from app.features.file.service import _clone_existing_file
        from app.models.file import File

        source = File(
            name="a.txt", file_path="hashfile2.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=1, parent_id=None,
            content_hash="b" * 64, status="pending",
            description=None, vector_info=None,
        )
        session.add(source)
        session.commit()

        with patch("app.features.file.service._push_processing_queue") as mock_q, \
             patch("app.features.file.service._clear_search_cache"):
            new_file = _clone_existing_file(
                session, source,
                filename="c.txt", workspace_id=test_workspace.id,
                uploader_id=1, parent_id=None,
                mime_type=None, content_hash="b" * 64,
            )
        assert new_file.status == "pending"
        assert new_file.description is None
        mock_q.assert_called_once()

    def test_clone_existing_file_error(self, session, test_workspace):
        from app.features.file.service import _clone_existing_file
        from app.models.file import File
        from app.exceptions import ServiceOperationError

        source = File(
            name="a.txt", file_path="hashfile3.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=1, parent_id=None,
            content_hash="c" * 64, status="success",
        )
        session.add(source)
        session.commit()

        with patch("app.features.file.service._persist_file_record", side_effect=Exception("DB error")):
            with pytest.raises(ServiceOperationError):
                _clone_existing_file(
                    session, source,
                    filename="d.txt", workspace_id=test_workspace.id,
                    uploader_id=1, parent_id=None,
                    mime_type="text/plain", content_hash="c" * 64,
                )


class TestFileServicePreflight:
    def test_preflight_no_filename(self, session, test_workspace):
        from app.features.file.service import preflight_file_upload
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            preflight_file_upload(session, test_workspace.id, 1, {"filename": "", "total_size": 10, "content_hash": "d" * 64})

    def test_preflight_no_size(self, session, test_workspace):
        from app.features.file.service import preflight_file_upload
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            preflight_file_upload(session, test_workspace.id, 1, {"filename": "a.txt", "total_size": 0, "content_hash": "d" * 64})

    def test_preflight_no_hash(self, session, test_workspace):
        from app.features.file.service import preflight_file_upload
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            preflight_file_upload(session, test_workspace.id, 1, {"filename": "a.txt", "total_size": 10, "content_hash": None})

    def test_preflight_no_source(self, session, test_workspace):
        from app.features.file.service import preflight_file_upload
        result = preflight_file_upload(session, test_workspace.id, 1, {
            "filename": "a.txt", "total_size": 10, "content_hash": "e" * 64
        })
        assert result["instant_upload"] is False

    def test_preflight_instant_upload(self, session, test_workspace, mock_object_storage):
        from app.features.file.service import preflight_file_upload
        from app.models.file import File

        source = File(
            name="orig.txt", file_path="hashfile4.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=1, parent_id=None,
            content_hash="f" * 64, status="success",
        )
        session.add(source)
        session.commit()
        mock_object_storage.objects[source.file_path] = b"0123456789"
        with patch("app.features.file.service._clear_search_cache"):
            result = preflight_file_upload(session, test_workspace.id, 1, {
                "filename": "new.txt", "total_size": 10, "content_hash": "f" * 64
            })
        assert result["instant_upload"] is True


class TestFileServiceCreate:
    def test_create_file(self, session, test_workspace, tmp_path):
        from app.features.file.service import create_file

        upload = MagicMock()
        upload.filename = "test_create.txt"
        upload.mimetype = "text/plain"
        upload.save = lambda p: open(p, "w").write("hello world")

        with patch("app.features.file.service._push_processing_queue"), \
             patch("app.features.file.service.UPLOAD_FOLDER", str(tmp_path)):
            new_file = create_file(session, upload, {
                "workspace_id": test_workspace.id, "uploader_id": 1, "parent_id": None
            })
        assert new_file.name == "test_create.txt"

    def test_create_uploaded_file_no_filename(self, session, test_workspace):
        from app.features.file.service import create_uploaded_file
        from app.exceptions import BusinessRuleError
        upload = MagicMock()
        upload.filename = None
        with pytest.raises(BusinessRuleError):
            create_uploaded_file(session, test_workspace.id, 1, upload)

    def test_create_uploaded_file_with_root(self, session, test_workspace, tmp_path):
        from app.features.file.service import create_uploaded_file
        from app.models.folder import Folder

        root = Folder(name="root", workspace_id=test_workspace.id, parent_id=None)
        session.add(root)
        session.commit()

        upload = MagicMock()
        upload.filename = "root_file.txt"
        upload.mimetype = "text/plain"
        upload.save = lambda p: open(p, "w").write("x")

        with patch("app.features.file.service._push_processing_queue"), \
             patch("app.features.file.service.UPLOAD_FOLDER", str(tmp_path)):
            f = create_uploaded_file(session, test_workspace.id, 1, upload)
        assert f is not None

    def test_batch_create_files_empty(self, session, test_workspace):
        from app.features.file.service import batch_create_files
        result = batch_create_files(session, [], {"workspace_id": test_workspace.id})
        assert result == []

    def test_batch_create_files(self, session, test_workspace, tmp_path):
        from app.features.file.service import batch_create_files

        upload1 = MagicMock()
        upload1.filename = "b1.txt"
        upload1.mimetype = "text/plain"
        upload1.save = lambda p: open(p, "w").write("hello")
        upload2 = MagicMock()
        upload2.filename = "b2.txt"
        upload2.mimetype = "text/plain"
        upload2.save = lambda p: open(p, "w").write("world")

        with patch("app.features.file.service._push_processing_queue"), \
             patch("app.features.file.service.UPLOAD_FOLDER", str(tmp_path)), \
             patch("app.features.folder.change_log.log_events_batch"):
            files = batch_create_files(session, [upload1, upload2], {"workspace_id": test_workspace.id, "uploader_id": 1})
        assert len(files) == 2

    def test_create_uploaded_files_no_valid(self, session, test_workspace):
        from app.features.file.service import create_uploaded_files
        from app.exceptions import BusinessRuleError
        upload = MagicMock()
        upload.filename = None
        with pytest.raises(BusinessRuleError):
            create_uploaded_files(session, test_workspace.id, 1, [upload])


class TestFileServiceMultipart:
    def test_init_multipart_upload(self, session, test_workspace, tmp_path):
        from app.features.file.service import init_multipart_upload
        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path / ".multipart")):
            data = {"filename": "big.txt", "total_size": 100, "chunk_size": 10}
            result = init_multipart_upload(session, test_workspace.id, 1, data)
        assert result["upload_id"] is not None
        assert result["instant_upload"] is False

    def test_init_multipart_no_filename(self, session, test_workspace):
        from app.features.file.service import init_multipart_upload
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            init_multipart_upload(session, test_workspace.id, 1, {"filename": "", "total_size": 100})

    def test_init_multipart_bad_size(self, session, test_workspace):
        from app.features.file.service import init_multipart_upload
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            init_multipart_upload(session, test_workspace.id, 1, {"filename": "a", "total_size": 0})

    def test_save_multipart_chunk(self, session, test_workspace, tmp_path):
        from app.features.file.service import init_multipart_upload, save_multipart_chunk
        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path / ".multipart")):
            data = {"filename": "chunk.txt", "total_size": 20, "chunk_size": 10}
            result = init_multipart_upload(session, test_workspace.id, 1, data)
            upload_id = result["upload_id"]

            chunk = MagicMock()
            chunk.save = lambda p: open(p, "wb").write(b"a" * 10)
            res = save_multipart_chunk(test_workspace.id, upload_id, 0, chunk)
        assert res["chunk_index"] == 0

    def test_complete_multipart_upload(self, session, test_workspace, tmp_path):
        from app.features.file.service import init_multipart_upload, save_multipart_chunk, complete_multipart_upload
        mp_root = str(tmp_path / ".multipart")
        up_folder = str(tmp_path / "uploads")
        os.makedirs(up_folder, exist_ok=True)
        with patch("app.features.file.service.MULTIPART_ROOT", mp_root), \
             patch("app.features.file.service.UPLOAD_FOLDER", up_folder):
            data = {"filename": "comp.txt", "total_size": 20, "chunk_size": 10}
            result = init_multipart_upload(session, test_workspace.id, 1, data)
            upload_id = result["upload_id"]
            for i in range(2):
                chunk = MagicMock()
                chunk.save = lambda p, d=b"x" * 10: open(p, "wb").write(d)
                save_multipart_chunk(test_workspace.id, upload_id, i, chunk)
            with patch("app.features.file.service._push_processing_queue"):
                f = complete_multipart_upload(session, test_workspace.id, 1, upload_id)
            assert f.name == "comp.txt"

    def test_abort_multipart_upload(self, session, test_workspace, tmp_path):
        from app.features.file.service import init_multipart_upload, abort_multipart_upload
        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path / ".multipart")):
            data = {"filename": "abort.txt", "total_size": 20, "chunk_size": 10}
            result = init_multipart_upload(session, test_workspace.id, 1, data)
            abort_multipart_upload(test_workspace.id, result["upload_id"])

    def test_get_multipart_status(self, session, test_workspace, tmp_path):
        from app.features.file.service import init_multipart_upload, get_multipart_upload_status
        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path / ".multipart")):
            data = {"filename": "stat.txt", "total_size": 20, "chunk_size": 10}
            result = init_multipart_upload(session, test_workspace.id, 1, data)
            status = get_multipart_upload_status(test_workspace.id, result["upload_id"])
        assert status["total_chunks"] == 2


class TestFileServiceSearch:
    def test_search_files_vector_no_config(self, session, test_workspace):
        from app.features.file.service import _search_files_vector
        with patch("app.features.file.service.get_embedding_model_config", side_effect=Exception("No config")):
            result = _search_files_vector(session, test_workspace.id, "test", 1, 10)
        assert "error" in result

    def test_get_files_and_folders(self, session, test_workspace):
        from app.features.file.service import get_files_and_folders
        from app.models.folder import Folder
        folder = Folder(name="gff", workspace_id=test_workspace.id, parent_id=None)
        session.add(folder)
        session.commit()
        result = get_files_and_folders(session, test_workspace.id, None, 1, 10)
        assert "folders" in result

    def test_get_files_and_folders_with_name(self, session, test_workspace):
        from app.features.file.service import get_files_and_folders
        from app.models.folder import Folder
        f1 = Folder(name="alpha", workspace_id=test_workspace.id, parent_id=None)
        f2 = Folder(name="beta", workspace_id=test_workspace.id, parent_id=None)
        session.add_all([f1, f2])
        session.commit()
        result = get_files_and_folders(session, test_workspace.id, None, 1, 10, name="alpha", sort_by="name", order="asc")
        assert len(result["folders"]) == 1

    def test_get_files_and_folders_sort_size(self, session, test_workspace):
        from app.features.file.service import get_files_and_folders
        result = get_files_and_folders(session, test_workspace.id, None, 1, 10, sort_by="size", order="asc")
        assert "folders" in result

    def test_get_all_files(self, session, test_workspace):
        from app.features.file.service import get_all_files
        result = get_all_files(session, test_workspace.id)
        assert isinstance(result, list)

    def test_embedding_desc(self):
        from app.features.file.service import embedding_desc
        with patch("app.infra.llm.client._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.data = [MagicMock(embedding=[0.1, 0.2])]
            mock_resp.usage = None
            mock_client.embeddings.create.return_value = mock_resp
            mock_gc.return_value = mock_client
            result = embedding_desc("test", {"api": "x", "key": "y", "model": "z"})
        assert result == [0.1, 0.2]

    def test_embedding_desc_error(self):
        from app.features.file.service import embedding_desc
        with patch("app.infra.llm.client._get_client", side_effect=Exception("fail")):
            result = embedding_desc("test", {"api": "x", "key": "y", "model": "z"})
        assert result == []

    def test_batch_embedding_desc(self):
        from app.features.file.service import batch_embedding_desc
        with patch("app.infra.llm.client._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.data = [MagicMock(embedding=[0.1], index=0), MagicMock(embedding=[0.2], index=1)]
            mock_resp.usage = None
            mock_client.embeddings.create.return_value = mock_resp
            mock_gc.return_value = mock_client
            result = batch_embedding_desc(["a", "b"], {"api": "x", "key": "y", "model": "z"})
        assert len(result) == 2

    def test_batch_embedding_desc_empty(self):
        from app.features.file.service import batch_embedding_desc
        assert batch_embedding_desc([], {}) == []

    def test_batch_embedding_desc_error(self):
        from app.features.file.service import batch_embedding_desc
        with patch("app.infra.llm.client._get_client", side_effect=Exception("fail")):
            result = batch_embedding_desc(["a", "b"], {"api": "x", "key": "y", "model": "z"})
        assert result == [[], []]

    def test_retry_embedding(self, session, test_workspace, tmp_path):
        from app.features.file.service import retry_embedding, create_file
        upload = MagicMock()
        upload.filename = "retry.txt"
        upload.mimetype = "text/plain"
        upload.save = lambda p: open(p, "w").write("x")
        with patch("app.features.file.service._push_processing_queue"), \
             patch("app.features.file.service.UPLOAD_FOLDER", str(tmp_path)):
            f = create_file(session, upload, {"workspace_id": test_workspace.id, "uploader_id": 1})
            retry_embedding(session, f.id)
            retry_embedding(session, 99999)

    def test_rebuild_failed_indexes(self, session, test_workspace):
        from app.features.file.service import rebuild_failed_indexes
        with patch("app.features.file.service.publish_file_tasks"):
            count = rebuild_failed_indexes(session, test_workspace.id)
        assert count == 0

    def test_cleanup_expired_uploads(self):
        from app.features.file.service import cleanup_expired_uploads, MULTIPART_ROOT
        if not os.path.exists(MULTIPART_ROOT):
            os.makedirs(MULTIPART_ROOT, exist_ok=True)
        result = cleanup_expired_uploads()
        assert isinstance(result, int)

    def test_delete_file(self, session, test_workspace, tmp_path):
        from app.features.file.service import create_file, delete_file
        upload = MagicMock()
        upload.filename = "del.txt"
        upload.mimetype = "text/plain"
        upload.save = lambda p: open(p, "w").write("delete me")
        with patch("app.features.file.service._push_processing_queue"), \
             patch("app.features.file.service.UPLOAD_FOLDER", str(tmp_path)):
            f = create_file(session, upload, {"workspace_id": test_workspace.id, "uploader_id": 1})
            delete_file(session, f.id)

    def test_delete_file_not_found(self, session):
        from app.features.file.service import delete_file
        from app.exceptions import ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError):
            delete_file(session, 99999)

    def test_batch_delete_items(self, session, test_workspace):
        from app.features.file.service import batch_delete_items
        from app.models.folder import Folder
        folder = Folder(name="bd", workspace_id=test_workspace.id, parent_id=None)
        session.add(folder)
        session.commit()
        with patch("app.features.folder.service.get_authorized_folder"), \
             patch("app.features.folder.service.delete_folder"):
            batch_delete_items(session, test_workspace.id, 1, [{"id": folder.id, "is_folder": True}])

    def test_get_root_file_id(self, session, test_workspace):
        from app.features.file.service import get_root_file_id
        result = get_root_file_id(session, test_workspace.id)
        assert result is None or isinstance(result, int)

    def test_get_authorized_file_wrong_ws(self, session, test_workspace):
        from app.features.file.service import get_authorized_file
        from app.models.file import File
        from app.exceptions import PermissionDeniedError
        f = File(name="af.txt", file_path="af.txt", file_size=1, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None)
        session.add(f)
        session.commit()
        with pytest.raises(PermissionDeniedError):
            get_authorized_file(session, 99999, 1, f.id)

    def test_get_downloadable_file_not_on_disk(self, session, test_workspace):
        from app.features.file.service import get_downloadable_file
        from app.models.file import File
        from app.exceptions import ResourceNotFoundError
        f = File(name="dl.txt", file_path="nonexist_dl.txt", file_size=1, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None)
        session.add(f)
        session.commit()
        with pytest.raises(ResourceNotFoundError):
            get_downloadable_file(session, test_workspace.id, 1, f.id)

    def test_update_file(self, session, test_workspace, tmp_path):
        from app.features.file.service import create_file, update_file
        upload = MagicMock()
        upload.filename = "up.txt"
        upload.mimetype = "text/plain"
        upload.save = lambda p: open(p, "w").write("x")
        with patch("app.features.file.service._push_processing_queue"), \
             patch("app.features.file.service.UPLOAD_FOLDER", str(tmp_path)), \
             patch("app.features.file.service._clear_search_cache"):
            f = create_file(session, upload, {"workspace_id": test_workspace.id, "uploader_id": 1})
            updated = update_file(session, f.id, {"name": "renamed.txt"})
        assert updated.name == "renamed.txt"

    def test_update_file_move(self, session, test_workspace, tmp_path):
        from app.features.file.service import create_file, update_file
        from app.models.folder import Folder
        upload = MagicMock()
        upload.filename = "mv.txt"
        upload.mimetype = "text/plain"
        upload.save = lambda p: open(p, "w").write("x")
        folder = Folder(name="target", workspace_id=test_workspace.id, parent_id=None)
        session.add(folder)
        session.commit()
        with patch("app.features.file.service._push_processing_queue"), \
             patch("app.features.file.service.UPLOAD_FOLDER", str(tmp_path)), \
             patch("app.features.file.service._clear_search_cache"):
            f = create_file(session, upload, {"workspace_id": test_workspace.id, "uploader_id": 1})
            updated = update_file(session, f.id, {"parent_id": folder.id})
        assert updated.parent_id == folder.id

    def test_search_files_empty_query(self, session, test_workspace):
        from app.features.file.service import search_files
        import asyncio
        result = asyncio.run(search_files(session, test_workspace.id, "", 1, 10, "fuzzy"))
        assert result["total"] == 0
