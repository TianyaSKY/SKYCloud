"""文件路由：上传/分片、列表检索、下载与索引重试。业务在 file_service。

注意：``GET /files/{id}`` 必须注册在所有具体路径之后，避免 ``{id}`` 吞掉 list/search 等。
"""

from typing import cast

from fastapi import (
    APIRouter,
    Depends,
    File as FastAPIFile,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi import UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.contracts import SuccessResponse, success_payload
from app.api.dependencies import get_current_user, get_current_workspace, require_workspace_role
from app.models.workspace import Workspace
from app.features.file.schemas import (
    FileResponse, DirectoryResponse, FilePageResponse, PreflightResponse,
    MultipartInitResponse, MultipartStatusResponse, MultipartChunkResponse,
    AvatarResponse, RebuildResponse, ProcessStatusResponse,
    BatchDeleteRequest,
    FilePreflightRequest,
    FileUpdateRequest,
    MultipartCompleteRequest,
    MultipartInitRequest,
    RetryEmbeddingRequest,
)
from app.infra.extensions import get_session_factory
from app.features.workspace.permissions import assert_member, assert_can_write_locked
from app.infra.upload_adapter import Base64UploadAdapter, FastAPIUploadAdapter
from app.infra.storage import get_storage_client
from app.features.file import service as file_service
from app.features.file.avatar_upload import upload_avatar_owned

router = APIRouter(tags=["file"])


@router.post("/files", response_model=SuccessResponse[FileResponse], status_code=status.HTTP_201_CREATED)
def create_file(
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        file: UploadFile = FastAPIFile(...),
        parent_id: int | None = Form(default=None),
        session_factory=Depends(get_session_factory),
):
    """单文件上传；未指定 parent_id 时落到工作空间根目录。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        new_file = file_service.create_uploaded_file(
            session, workspace.id, current_user.id, FastAPIUploadAdapter(file), parent_id
        )
        return success_payload(new_file.to_dict())


@router.post("/files/batch", response_model=SuccessResponse[list[FileResponse]], status_code=status.HTTP_201_CREATED)
def batch_upload_files(
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        files: list[UploadFile] | None = FastAPIFile(default=None),
        parent_id: int | None = Form(default=None),
        session_factory=Depends(get_session_factory),
):
    """批量上传；未指定 parent_id 时落到工作空间根目录。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        adapters = [FastAPIUploadAdapter(file) for file in (files or []) if file]
        new_files = file_service.create_uploaded_files(
            session, workspace.id, current_user.id, adapters, parent_id
        )
        return success_payload([f.to_dict() for f in new_files])


@router.post("/files/preflight", response_model=SuccessResponse[PreflightResponse])
def preflight_file_upload(
        payload: FilePreflightRequest,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        session_factory=Depends(get_session_factory),
):
    """上传前校验（哈希秒传、配额等），避免无效分片开销。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        return success_payload(file_service.preflight_file_upload(
            session, workspace.id, current_user.id, payload.model_dump(exclude_none=True)
        ))


@router.post("/files/multipart/init", response_model=SuccessResponse[MultipartInitResponse])
def init_multipart_upload(
        payload: MultipartInitRequest,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        session_factory=Depends(get_session_factory),
):
    """初始化分片上传会话。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        return success_payload(file_service.init_multipart_upload(
            session, workspace.id, current_user.id, payload.model_dump(exclude_none=True)
        ))


@router.post("/files/multipart/chunk", response_model=SuccessResponse[MultipartChunkResponse])
def upload_multipart_chunk(
        upload_id: str = Form(...),
        chunk_index: int = Form(...),
        chunk: UploadFile = FastAPIFile(...),
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        session_factory=Depends(get_session_factory),
):
    """写入单个分片。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        if not chunk.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="No chunk provided"
            )

        return success_payload(file_service.save_multipart_chunk(
            session,
            current_user.id,
            workspace.id,
            upload_id,
            chunk_index,
            FastAPIUploadAdapter(chunk),
        ))


@router.post("/files/multipart/complete", response_model=SuccessResponse[FileResponse], status_code=status.HTTP_201_CREATED)
def complete_multipart_upload(
        payload: MultipartCompleteRequest,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        session_factory=Depends(get_session_factory),
):
    """合并分片并落库，触发后续索引任务。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        new_file = file_service.complete_multipart_upload(
            session, workspace.id, current_user.id, payload.upload_id
        )
        return success_payload(new_file.to_dict())


@router.get("/files/multipart/{upload_id}", response_model=SuccessResponse[MultipartStatusResponse])
def get_multipart_upload_status(
        upload_id: str,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        session_factory=Depends(get_session_factory),
):
    """查询分片上传进度（已收分片等）。"""
    with session_factory() as session:
        assert_member(session, workspace.id, current_user.id)
        return success_payload(file_service.get_multipart_upload_status(session, workspace.id, current_user.id, upload_id))


@router.delete("/files/multipart/{upload_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def abort_multipart_upload(
        upload_id: str,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        session_factory=Depends(get_session_factory),
):
    """中止并清理未完成的分片会话。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        file_service.abort_multipart_upload(session, workspace.id, current_user.id, upload_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/files/list", response_model=SuccessResponse[DirectoryResponse])
def list_files(
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        parent_id: int | None = Query(default=None, ge=1),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, ge=1, le=100),
        name: str | None = Query(default=None, max_length=255),
        sort_by: str = Query(default="created_at", min_length=1, max_length=50),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        session_factory=Depends(get_session_factory),
):
    """目录浏览：文件与子文件夹分页列表。"""
    with session_factory() as session:
        assert_member(session, workspace.id, current_user.id)
        return success_payload(file_service.get_files_and_folders(
            session, workspace.id, parent_id, page, page_size, name, sort_by, order
        ))


@router.get("/files/search", response_model=SuccessResponse[FilePageResponse])
async def search_files(
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        q: str = Query(default="", max_length=255),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, ge=1, le=100),
        type: str = Query(default="fuzzy", pattern="^(fuzzy|semantic)$"),
        session_factory=Depends(get_session_factory),
):
    """文件名模糊或语义检索；空查询直接返回空页避免全表扫描。"""
    if not q:
        return success_payload({"items": [], "total": 0, "page": page, "page_size": page_size})
    result = await file_service.search_files(None, workspace.id, q, page, page_size, type,
                                             session_factory=session_factory, user_id=current_user.id)
    if result.get('error'):
        from app.exceptions import ServiceOperationError
        raise ServiceOperationError('File search unavailable')
    return success_payload(result)


@router.put("/files/{id}", response_model=SuccessResponse[FileResponse])
def update_file(
        id: int,
        payload: FileUpdateRequest,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        session_factory=Depends(get_session_factory),
):
    """重命名/移动文件等元数据更新。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        file_service.get_authorized_file(session, workspace.id, current_user.id, id)
        file_obj = file_service.update_file(session, id, payload.model_dump(exclude_none=True))
        return success_payload(file_obj.to_dict())


@router.delete("/files/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_file(
        id: int,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        session_factory=Depends(get_session_factory),
):
    """删除单个文件。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        file_service.get_authorized_file(session, workspace.id, current_user.id, id)
        file_service.delete_file(session, id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/files/{id}/download", response_class=StreamingResponse, responses={200: {'description': 'Authenticated original file bytes'}})
def download_file(
        id: int,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        session_factory=Depends(get_session_factory),
):
    """鉴权后以附件形式下载原始文件（从 MinIO 流式读取）。"""
    with session_factory() as session:
        assert_member(session, workspace.id, current_user.id)
        file_obj = file_service.get_downloadable_file(
            session, workspace.id, current_user.id, id
        )
        storage = get_storage_client()
        object_name = cast(str, file_obj.file_path)
        file_stream = storage.get_file_stream(object_name)

        from urllib.parse import quote
        filename = cast(str | None, file_obj.name) or "download"
        encoded_filename = quote(filename)

        return StreamingResponse(
            file_stream,
            media_type=cast(str | None, file_obj.mime_type) or "application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            },
            background=BackgroundTask(storage.close_file_stream, file_stream),
        )


@router.post("/files/upload/avatar/{id}", response_model=SuccessResponse[AvatarResponse])
async def upload_avatar(
        id: int,
        request: Request,
        avatar: UploadFile | None = FastAPIFile(default=None),
        avatar_base64: str | None = Form(default=None),
        current_user=Depends(get_current_user),
        session_factory=Depends(get_session_factory),
):
    """上传用户头像；兼容 multipart 文件、form base64 与 JSON body。"""
    avatar_text = None
    if avatar is not None:
        if not avatar.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="No avatar file"
            )
    else:
        avatar_text = (avatar_base64 or "").strip()
        if not avatar_text and request.headers.get("content-type", "").startswith(
                "application/json"
        ):
            try:
                payload = await request.json()
                if isinstance(payload, dict):
                    avatar_text = str(payload.get("avatar") or "").strip()
            except Exception:
                avatar_text = ""

        if not avatar_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="No avatar data"
            )

    result = await run_in_threadpool(
        upload_avatar_owned, session_factory, current_user.id, id,
        upload=avatar, encoded=avatar_text
    )
    # 兼容旧前端字段名 avatar
    if result.get("avatar_url") and "avatar" not in result:
        result["avatar"] = result["avatar_url"]
    return success_payload(result)


@router.post("/files/batch-delete", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def batch_delete_files(
        payload: BatchDeleteRequest,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(require_workspace_role("editor")),
        session_factory=Depends(get_session_factory),
):
    """批量删除文件与/或文件夹。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        items = [item.model_dump() for item in payload.items]
        file_service.batch_delete_items(session, workspace.id, current_user.id, items)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/files/retry_embedding", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def retry_embedding(
        payload: RetryEmbeddingRequest,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        session_factory=Depends(get_session_factory),
):
    """对单个失败文件重新入队索引。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        file_obj = file_service.get_authorized_file(
            session, workspace.id, current_user.id, payload.file_id
        )
        file_service.retry_embedding(session, cast(int, file_obj.id))
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/files/rebuild_failed_indexes", response_model=SuccessResponse[RebuildResponse])
def rebuild_failed_indexes(
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        session_factory=Depends(get_session_factory),
):
    """批量重试当前工作空间所有失败索引。"""
    with session_factory() as session:
        assert_can_write_locked(session, workspace.id, current_user.id)
        count = file_service.rebuild_failed_indexes(session, workspace.id)
        return success_payload({"count": count})


@router.get("/files/process_status", response_model=SuccessResponse[ProcessStatusResponse])
async def process_status(
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        session_factory=Depends(get_session_factory),
):
    """查询当前工作空间文件处理状态汇总（pending/processing 等）。"""
    return success_payload(await file_service.process_status(None, workspace.id,
                           session_factory=session_factory, user_id=current_user.id))


# 必须放在所有 /files/* 具体路由之后，避免 {id} 匹配 list、search 等路径段
@router.get("/files/{id}", response_model=SuccessResponse[FileResponse])
def get_file(
        id: int,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        session_factory=Depends(get_session_factory),
):
    """获取单文件元数据（含权限校验）。"""
    with session_factory() as session:
        assert_member(session, workspace.id, current_user.id)
        file_obj = file_service.get_authorized_file(
            session, workspace.id, current_user.id, id
        )
        return success_payload(file_obj.to_dict())
