"""file 上传用例与文件服务完整测试。"""
import asyncio
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import UploadFile
import pytest
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from app.exceptions import (
    ConflictError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ServiceOperationError,
)
from app.features.file.multipart_service import (
    MultipartInitData,
    SaveMultipartChunkParams,
    get_multipart_service,
)
from app.features.file.search_service import SearchFilesParams, embedding_desc, search_files
from app.features.file.service import (
    BatchDeleteItemData,
    GetFilesAndFoldersParams,
    batch_delete_items,
    cleanup_expired_uploads,
    delete_file,
    get_all_files,
    get_authorized_file,
    get_downloadable_file,
    get_files_and_folders,
    get_root_file_id,
    rebuild_failed_indexes,
    retry_embedding,
    update_file,
)
from app.features.file.upload_service import CloneSourceParams, get_upload_service
from app.features.file.upload_types import PreflightData, UploadSource
from app.infra.upload_adapter import FastAPIUploadAdapter
from app.models import File, Folder, UploadReservation, User, Workspace


def _upload(filename: str, payload: bytes, mimetype: str = "text/plain") -> FastAPIUploadAdapter:
    headers = Headers({"content-type": mimetype}) if mimetype else None
    return FastAPIUploadAdapter(UploadFile(
        filename=filename, file=BytesIO(payload), headers=headers,
    ))


def _create_file(session: Session, workspace_id: int, uploader_id: int, upload_root: Path, filename: str = "f.txt") -> File:
    """用公开上传用例创建一条已提交的文件记录。"""
    with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(upload_root)):
        return get_upload_service().create_uploaded_file(
            session, workspace_id, uploader_id, _upload(filename, b"x"),
        )


class TestFileServiceClone:
    def test_clone_existing_file_success(
        self, session: Session, test_workspace: Workspace, test_user: User, mock_object_storage: Any,
    ) -> None:
        source = File(
            name="a.txt", file_path="hashfile.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=test_user.id, parent_id=None,
            content_hash="a" * 64, status="success",
            description="desc", vector_info=None,
        )
        session.add(source)
        session.commit()
        mock_object_storage.objects[source.file_path] = b"0123456789"

        new_file = get_upload_service().clone_source(session, CloneSourceParams(
            source_file=source, filename="b.txt", workspace_id=test_workspace.id,
            uploader_id=test_user.id, parent_id=None,
            mime_type="text/plain", content_hash="a" * 64,
        ))
        assert new_file.name == "b.txt"
        assert new_file.status == "success"
        assert new_file.description == "desc"
        persisted = session.get(File, new_file.id)
        assert persisted is not None
        assert persisted.name == "b.txt"
        assert persisted.file_path == "hashfile.txt"

    def test_clone_existing_file_pending(
        self, session: Session, test_workspace: Workspace, test_user: User, mock_object_storage: Any,
    ) -> None:
        source = File(
            name="a.txt", file_path="hashfile2.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=test_user.id, parent_id=None,
            content_hash="b" * 64, status="pending",
            description=None, vector_info=None,
        )
        session.add(source)
        session.commit()
        mock_object_storage.objects[source.file_path] = b"0123456789"

        new_file = get_upload_service().clone_source(session, CloneSourceParams(
            source_file=source, filename="c.txt", workspace_id=test_workspace.id,
            uploader_id=test_user.id, parent_id=None,
            mime_type=None, content_hash="b" * 64,
        ))
        assert new_file.status == "pending"
        assert new_file.description is None
        persisted = session.get(File, new_file.id)
        assert persisted is not None
        assert persisted.status == "pending"

    def test_clone_rejects_source_removed_before_publication(
        self, session: Session, test_workspace: Workspace, test_user: User, mock_object_storage: Any,
    ) -> None:
        source = File(
            name="a.txt", file_path="hashfile3.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=test_user.id, parent_id=None,
            content_hash="c" * 64, status="success",
        )
        session.add(source)
        session.commit()
        source_path = source.file_path
        mock_object_storage.objects[source_path] = b"0123456789"
        stale_source = File(
            id=source.id, name=source.name, file_path=source.file_path,
            file_size=source.file_size, mime_type=source.mime_type,
            workspace_id=source.workspace_id, uploader_id=source.uploader_id,
            parent_id=source.parent_id, content_hash=source.content_hash,
            status=source.status,
        )
        session.delete(source)
        session.commit()

        with pytest.raises(ConflictError, match="no longer available"):
            get_upload_service().clone_source(session, CloneSourceParams(
                source_file=stale_source, filename="d.txt",
                workspace_id=test_workspace.id, uploader_id=test_user.id,
                parent_id=None, mime_type="text/plain", content_hash="c" * 64,
            ))

        session.expire_all()
        assert session.query(File).count() == 0
        assert mock_object_storage.objects[source_path] == b"0123456789"


    def test_clone_database_failure_still_cancels_the_reservation(
        self, session: Session, test_workspace: Workspace, test_user: User, mock_object_storage: Any,
    ) -> None:
        source = File(
            name="a.txt", file_path="hashfile-fail.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=test_user.id, parent_id=None,
            content_hash="d" * 64, status="success",
        )
        session.add(source)
        session.commit()
        mock_object_storage.objects[source.file_path] = b"0123456789"

        def failing_publication(sess: Session, spec: Any) -> None:
            # 真实的 flush 失败会回滚事务并使附加的预留失效;
            # 处理器在此之后绝不得读取该预留.
            sess.add(File(
                name=cast(Any, None), file_path="invalid", file_size=1,
                workspace_id=spec.workspace_id, uploader_id=spec.uploader_id,
            ))
            sess.flush()

        with patch(
            "app.features.file.upload_service.stage_record",
            side_effect=failing_publication,
        ):
            with pytest.raises(ServiceOperationError, match="Failed to save file metadata"):
                get_upload_service().clone_source(session, CloneSourceParams(
                    source_file=source, filename="e.txt",
                    workspace_id=test_workspace.id, uploader_id=test_user.id,
                    parent_id=None, mime_type="text/plain", content_hash="d" * 64,
                ))

        reservation = session.query(UploadReservation).filter_by(
            workspace_id=test_workspace.id, status="cancelled",
        ).one()
        assert reservation.kind == "instant"
        assert session.query(File).filter_by(name="e.txt").count() == 0
        assert mock_object_storage.objects[source.file_path] == b"0123456789"


class TestFileServicePreflight:
    def test_preflight_instant_upload(
        self, session: Session, test_workspace: Workspace, test_user: User, mock_object_storage: Any,
    ) -> None:
        source = File(
            name="orig.txt", file_path="hashfile4.txt", file_size=10,
            mime_type="text/plain", workspace_id=test_workspace.id,
            uploader_id=test_user.id, parent_id=None,
            content_hash="f" * 64, status="success",
        )
        session.add(source)
        session.commit()
        mock_object_storage.objects[source.file_path] = b"0123456789"

        result = get_upload_service().preflight_file_upload(session, test_workspace.id, test_user.id, cast(PreflightData, {
            # 大写哈希应被规范化为小写后命中
            "filename": "new.txt", "total_size": 10, "content_hash": "F" * 64
        }))
        assert result["instant_upload"] is True
        file_dict = cast(dict[str, Any], result["file"])
        f_obj = session.get(File, file_dict["id"])
        assert f_obj is not None
        assert f_obj.name == "new.txt"


class TestFileServiceMultipart:
    def test_save_multipart_chunk(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            service = get_multipart_service()
            result = service.init_multipart_upload(
                session, test_workspace.id, test_user.id,
                cast(MultipartInitData, {"filename": "chunk.txt", "total_size": 20, "chunk_size": 10}),
            )
            upload_id = cast(str, result["upload_id"])
            res = service.save_multipart_chunk(session, SaveMultipartChunkParams(
                uploader_id=test_user.id, workspace_id=test_workspace.id,
                upload_id=upload_id, chunk_index=0,
                chunk=cast(UploadSource, _upload("chunk", b"a" * 10)),
            ))
        assert res["chunk_index"] == 0
        assert res["uploaded_chunks"] == [0]

    def test_complete_multipart_upload(
        self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path, mock_object_storage: Any,
    ) -> None:
        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            service = get_multipart_service()
            result = service.init_multipart_upload(
                session, test_workspace.id, test_user.id,
                cast(MultipartInitData, {"filename": "comp.txt", "total_size": 20, "chunk_size": 10}),
            )
            upload_id = cast(str, result["upload_id"])
            for i in range(2):
                service.save_multipart_chunk(session, SaveMultipartChunkParams(
                    uploader_id=test_user.id, workspace_id=test_workspace.id,
                    upload_id=upload_id, chunk_index=i,
                    chunk=cast(UploadSource, _upload("chunk", b"x" * 10)),
                ))
            f = service.complete_multipart_upload(
                session, test_workspace.id, test_user.id, upload_id,
            )
        assert f.name == "comp.txt"
        assert f.file_size == 20
        stored = session.get(File, f.id)
        assert stored is not None
        assert stored.name == "comp.txt"
        assert mock_object_storage.objects[f.file_path] == b"x" * 20
        assert not (tmp_path / ".multipart" / str(test_workspace.id) / upload_id).exists()


class TestFileServiceSearch:
    async def test_search_files_vector_no_config(self, session: Session, test_workspace: Workspace) -> None:
        with patch("app.features.file.search_service.get_embedding_model_config", side_effect=Exception("No config")):
            result = await search_files(session, test_workspace.id, SearchFilesParams(query="test", page=1, page_size=10, search_type="vector"))
        assert "error" in result

    def test_get_files_and_folders(self, session: Session, test_workspace: Workspace) -> None:
        folder = Folder(name="gff", workspace_id=test_workspace.id, parent_id=None)
        session.add(folder)
        session.commit()
        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=None, page=1, page_size=10))
        assert "folders" in result

    def test_get_files_and_folders_with_name(self, session: Session, test_workspace: Workspace) -> None:
        f1 = Folder(name="alpha", workspace_id=test_workspace.id, parent_id=None)
        f2 = Folder(name="beta", workspace_id=test_workspace.id, parent_id=None)
        session.add_all([f1, f2])
        session.commit()
        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=None, page=1, page_size=10, name="alpha", sort_by="name", order="asc"))
        assert len(cast(list[Any], result["folders"])) == 1

    def test_get_files_and_folders_sort_size(self, session: Session, test_workspace: Workspace) -> None:
        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=None, page=1, page_size=10, sort_by="size", order="asc"))
        assert "folders" in result

    def test_get_all_files(self, session: Session, test_workspace: Workspace) -> None:
        result = get_all_files(session, test_workspace.id)
        assert isinstance(result, list)

    async def test_embedding_desc(self) -> None:
        with patch("app.infra.llm.client._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.data = [MagicMock(embedding=[0.1, 0.2])]
            mock_resp.usage = None
            mock_client.embeddings.create = AsyncMock(return_value=mock_resp)
            mock_gc.return_value = mock_client
            result = await embedding_desc("test", {"api": "x", "key": "y", "model": "z"})
        assert result == [0.1, 0.2]

    async def test_embedding_desc_error(self) -> None:
        with patch("app.infra.llm.client._get_client", side_effect=Exception("fail")):
            result = await embedding_desc("test", {"api": "x", "key": "y", "model": "z"})
        assert result == []

    def test_retry_embedding(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        f = _create_file(session, test_workspace.id, test_user.id, tmp_path, "retry.txt")
        retry_embedding(session, f.id)
        retry_embedding(session, 99999)

    def test_rebuild_failed_indexes(self, session: Session, test_workspace: Workspace) -> None:
        with patch("app.features.file.service.stage_file_tasks"):
            count = rebuild_failed_indexes(session, test_workspace.id)
        assert count == 0

    def test_cleanup_expired_uploads(self, tmp_path: Path) -> None:
        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            result = cleanup_expired_uploads()
        assert isinstance(result, int)

    def test_delete_file(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        f = _create_file(session, test_workspace.id, test_user.id, tmp_path, "del.txt")
        delete_file(session, f.id)

    def test_delete_file_not_found(self, session: Session) -> None:
        with pytest.raises(ResourceNotFoundError):
            delete_file(session, 99999)

    def test_batch_delete_items(self, session: Session, test_workspace: Workspace) -> None:
        folder = Folder(name="bd", workspace_id=test_workspace.id, parent_id=None)
        session.add(folder)
        session.commit()
        with patch("app.features.folder.service.get_authorized_folder"), \
             patch("app.features.folder.service.delete_folder"):
            batch_delete_items(session, test_workspace.id, 1, [cast(BatchDeleteItemData, {"id": folder.id, "is_folder": True})])

    def test_get_root_file_id(self, session: Session, test_workspace: Workspace) -> None:
        result = get_root_file_id(session, test_workspace.id)
        assert result is None or isinstance(result, int)

    def test_get_authorized_file_wrong_ws(self, session: Session, test_workspace: Workspace) -> None:
        f = File(name="af.txt", file_path="af.txt", file_size=1, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None)
        session.add(f)
        session.commit()
        with pytest.raises(PermissionDeniedError):
            get_authorized_file(session, 99999, 1, f.id)

    def test_get_downloadable_file_not_on_disk(self, session: Session, test_workspace: Workspace) -> None:
        f = File(name="dl.txt", file_path="nonexist_dl.txt", file_size=1, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None)
        session.add(f)
        session.commit()
        with pytest.raises(ResourceNotFoundError):
            get_downloadable_file(session, test_workspace.id, 1, f.id)

    def test_update_file(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        f = _create_file(session, test_workspace.id, test_user.id, tmp_path, "up.txt")
        with patch("app.features.file.service.clear_search_cache"):
            updated = update_file(session, f.id, {"name": "renamed.txt"})
        assert updated.name == "renamed.txt"

    def test_update_file_move(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        folder = Folder(name="target", workspace_id=test_workspace.id, parent_id=None)
        session.add(folder)
        session.commit()
        f = _create_file(session, test_workspace.id, test_user.id, tmp_path, "mv.txt")
        with patch("app.features.file.service.clear_search_cache"):
            updated = update_file(session, f.id, {"parent_id": folder.id})
        assert updated.parent_id == folder.id

    def test_search_files_empty_query(self, session: Session, test_workspace: Workspace) -> None:
        result = asyncio.run(search_files(session, test_workspace.id, SearchFilesParams(query="", page=1, page_size=10, search_type="fuzzy")))
        assert cast(int, result["total"]) == 0

