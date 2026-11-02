"""文件路由接口: 上传、分片、列表、下载与索引重试.

``GET /files/{id}`` 必须在所有具体 ``/files/*`` 路径之后注册,
避免 ``{id}`` 路径参数误匹配 ``list`` 或 ``search``。
"""

from typing import TYPE_CHECKING, Annotated, cast
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import (
    File as FastAPIFile,
)
from fastapi import (
    Path as PathParam,
)
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.contracts import SuccessEnvelope, SuccessResponse, success_payload
from app.api.dependencies import Ctx, get_current_workspace, require_workspace_role
from app.exceptions import ServiceOperationError
from app.features.file import search_service
from app.features.file import service as file_service
from app.features.file.avatar_upload import upload_avatar_owned
from app.features.file.multipart_service import (
    MultipartInitData,
    SaveMultipartChunkParams,
    get_multipart_service,
)
from app.features.file.schemas import (
    AvatarResponse,
    BatchDeleteRequest,
    DirectoryResponse,
    FileListQuery,
    FilePageResponse,
    FilePreflightRequest,
    FileResponse,
    FileSearchQuery,
    FileUpdateRequest,
    MultipartChunkResponse,
    MultipartCompleteRequest,
    MultipartInitRequest,
    MultipartInitResponse,
    MultipartStatusResponse,
    PreflightResponse,
    ProcessStatusResponse,
    RebuildResponse,
    RetryEmbeddingRequest,
)
from app.features.file.search_service import SearchFilesParams
from app.features.file.service import (
    BatchDeleteItemData,
    FileUpdateData,
    GetFilesAndFoldersParams,
)
from app.features.file.upload_service import get_upload_service
from app.features.workspace.permissions import (
    assert_can_write_locked,
    assert_member,
)
from app.infra.owned_work import run_owned
from app.infra.storage import get_storage_client
from app.infra.upload_adapter import FastAPIUploadAdapter
from app.models.file import FileDict
from app.models.workspace import Workspace

if TYPE_CHECKING:
    from app.features.file.upload_types import PreflightData, UploadSource

router = APIRouter(tags=["file"])


def _update_data(payload: FileUpdateRequest) -> FileUpdateData:
    """将请求模型转换为实际提供的元数据字段字典."""
    data: FileUpdateData = {}
    if payload.name is not None:
        data["name"] = payload.name
    if payload.status is not None:
        data["status"] = payload.status
    if payload.parent_id is not None:
        data["parent_id"] = payload.parent_id
    return data


@router.post(
    "/files",
    response_model=SuccessResponse[FileResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_file(
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
    file: Annotated[UploadFile, FastAPIFile(...)],
    parent_id: Annotated[int | None, Form()] = None,
) -> SuccessEnvelope[FileDict]:
    """上传单个文件; 未指定 parent_id 时默认放入工作空间根目录."""

    def upload() -> SuccessEnvelope[FileDict]:
        with ctx.session_factory() as session:
            assert_can_write_locked(session, workspace.id, ctx.user.id)
            new_file = get_upload_service().create_uploaded_file(
                session,
                workspace.id,
                ctx.user.id,
                FastAPIUploadAdapter(file),
                parent_id,
            )
            return success_payload(new_file.to_dict())

    return await run_owned(upload)


@router.post(
    "/files/batch",
    response_model=SuccessResponse[list[FileResponse]],
    status_code=status.HTTP_201_CREATED,
)
async def batch_upload_files(
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
    files: Annotated[list[UploadFile] | None, FastAPIFile()] = None,
    parent_id: Annotated[int | None, Form()] = None,
) -> SuccessEnvelope[list[FileDict]]:
    """批量上传文件; 未指定 parent_id 时默认放入工作空间根目录."""

    def upload() -> SuccessEnvelope[list[FileDict]]:
        with ctx.session_factory() as session:
            assert_can_write_locked(session, workspace.id, ctx.user.id)
            adapters: list[UploadSource] = [
                FastAPIUploadAdapter(file) for file in (files or []) if file
            ]
            new_files = get_upload_service().create_uploaded_files(
                session, workspace.id, ctx.user.id, adapters, parent_id,
            )
            return success_payload([file.to_dict() for file in new_files])

    return await run_owned(upload)


@router.post("/files/preflight", response_model=SuccessResponse[PreflightResponse])
def preflight_file_upload(
    payload: FilePreflightRequest,
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
) -> SuccessEnvelope[dict[str, object]]:
    """在分片传输开始前检查文件哈希秒传命中与配额."""
    data: PreflightData = {
        "filename": payload.filename,
        "total_size": payload.total_size,
        "content_hash": payload.content_hash,
    }
    if payload.parent_id is not None:
        data["parent_id"] = payload.parent_id
    if payload.mime_type is not None:
        data["mime_type"] = payload.mime_type
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        return success_payload(
            get_upload_service().preflight_file_upload(
                session, workspace.id, ctx.user.id, data,
            ),
        )


@router.post(
    "/files/multipart/init", response_model=SuccessResponse[MultipartInitResponse],
)
def init_multipart_upload(
    payload: MultipartInitRequest,
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
) -> SuccessEnvelope[dict[str, object]]:
    """开启用于接收后续上传分片的分片上传会话."""
    data: MultipartInitData = {
        "filename": payload.filename,
        "total_size": payload.total_size,
    }
    if payload.chunk_size is not None:
        data["chunk_size"] = payload.chunk_size
    if payload.parent_id is not None:
        data["parent_id"] = payload.parent_id
    if payload.mime_type is not None:
        data["mime_type"] = payload.mime_type
    if payload.content_hash is not None:
        data["content_hash"] = payload.content_hash
    if payload.upload_id is not None:
        data["upload_id"] = payload.upload_id
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        return success_payload(
            get_multipart_service().init_multipart_upload(
                session, workspace.id, ctx.user.id, data,
            ),
        )


@router.post(
    "/files/multipart/chunk", response_model=SuccessResponse[MultipartChunkResponse],
)
async def upload_multipart_chunk(
    upload_id: Annotated[str, Form()],
    chunk_index: Annotated[int, Form()],
    chunk: Annotated[UploadFile, FastAPIFile(...)],
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
    ctx: Ctx,
) -> SuccessEnvelope[dict[str, object]]:
    """保存进行中分片会话的单个上传分片数据."""

    def upload() -> SuccessEnvelope[dict[str, object]]:
        with ctx.session_factory() as session:
            assert_can_write_locked(session, workspace.id, ctx.user.id)
            if not chunk.filename:
                message = "No chunk provided"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=message,
                )

            return success_payload(
                get_multipart_service().save_multipart_chunk(
                    session,
                    SaveMultipartChunkParams(
                        uploader_id=ctx.user.id,
                        workspace_id=workspace.id,
                        upload_id=upload_id,
                        chunk_index=chunk_index,
                        chunk=FastAPIUploadAdapter(chunk),
                    ),
                ),
            )

    return await run_owned(upload)


@router.post(
    "/files/multipart/complete",
    response_model=SuccessResponse[FileResponse],
    status_code=status.HTTP_201_CREATED,
)
def complete_multipart_upload(
    payload: MultipartCompleteRequest,
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
) -> SuccessEnvelope[FileDict]:
    """合并全部已上传分片, 持久化文件记录并触发异步索引任务."""
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        new_file = get_multipart_service().complete_multipart_upload(
            session, workspace.id, ctx.user.id, payload.upload_id,
        )
        return success_payload(new_file.to_dict())


@router.get(
    "/files/multipart/{upload_id}",
    response_model=SuccessResponse[MultipartStatusResponse],
)
def get_multipart_upload_status(
    upload_id: str,
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
) -> SuccessEnvelope[dict[str, object]]:
    """上报当前分片上传会话中已成功接收的分片编号列表."""
    with ctx.session_factory() as session:
        assert_member(session, workspace.id, ctx.user.id)
        return success_payload(
            get_multipart_service().get_multipart_upload_status(
                session, workspace.id, ctx.user.id, upload_id,
            ),
        )


@router.delete(
    "/files/multipart/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def abort_multipart_upload(
    upload_id: str,
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
) -> Response:
    """中止分片上传会话并清理未完成的分片暂存目录."""
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        get_multipart_service().abort_multipart_upload(
            session, workspace.id, ctx.user.id, upload_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/files/list", response_model=SuccessResponse[DirectoryResponse])
def list_files(
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
    query: Annotated[FileListQuery, Query()],
) -> SuccessEnvelope[dict[str, object]]:
    """浏览目录内容: 文件夹置顶, 其后紧跟单页文件列表."""
    with ctx.session_factory() as session:
        assert_member(session, workspace.id, ctx.user.id)
        return success_payload(
            file_service.get_files_and_folders(
                session,
                GetFilesAndFoldersParams(
                    workspace_id=workspace.id,
                    parent_id=query.parent_id,
                    page=query.page,
                    page_size=query.page_size,
                    name=query.name,
                    sort_by=query.sort_by,
                    order=query.order,
                ),
            ),
        )


@router.get("/files/search", response_model=SuccessResponse[FilePageResponse])
async def search_files(
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
    query: Annotated[FileSearchQuery, Query()],
) -> SuccessEnvelope[dict[str, object]]:
    """基于文件名或向量嵌入搜索; 检索词为空时返回空结果页."""
    if not query.q:
        return success_payload(
            {
                "items": [],
                "total": 0,
                "page": query.page,
                "page_size": query.page_size,
            },
        )
    result = await search_service.search_files(
        None,
        workspace.id,
        SearchFilesParams(
            query=query.q,
            page=query.page,
            page_size=query.page_size,
            search_type=query.search_type,
        ),
        session_factory=ctx.session_factory,
        user_id=ctx.user.id,
    )
    if result.get("error"):
        message = "File search unavailable"
        raise ServiceOperationError(message)
    return success_payload(result)


@router.put("/files/{id}", response_model=SuccessResponse[FileResponse])
def update_file(
    file_id: Annotated[int, PathParam(alias="id")],
    payload: FileUpdateRequest,
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
) -> SuccessEnvelope[FileDict]:
    """重命名、移动或更新单个文件的状态."""
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        file_service.get_authorized_file(
            session, workspace.id, ctx.user.id, file_id,
        )
        file_obj = file_service.update_file(session, file_id, _update_data(payload))
        return success_payload(file_obj.to_dict())


@router.delete(
    "/files/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
)
def delete_file(
    file_id: Annotated[int, PathParam(alias="id")],
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
) -> Response:
    """删除单条文件记录并暂存其底层对象存储清理任务."""
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        file_service.get_authorized_file(
            session, workspace.id, ctx.user.id, file_id,
        )
        file_service.delete_file(session, file_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/files/{id}/download",
    response_class=StreamingResponse,
    responses={200: {"description": "Authenticated original file bytes"}},
)
def download_file(
    file_id: Annotated[int, PathParam(alias="id")],
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
) -> StreamingResponse:
    """完成权限校验后以附件形式流式传输原始对象数据."""
    with ctx.session_factory() as session:
        assert_member(session, workspace.id, ctx.user.id)
        file_obj = file_service.get_downloadable_file(
            session, workspace.id, ctx.user.id, file_id,
        )
        storage = get_storage_client()
        file_stream = storage.get_file_stream(file_obj.file_path)

        filename = file_obj.name or "download"
        encoded_filename = quote(filename)

        disposition = f"attachment; filename*=UTF-8''{encoded_filename}"
        return StreamingResponse(
            file_stream,
            media_type=file_obj.mime_type or "application/octet-stream",
            headers={"Content-Disposition": disposition},
            background=BackgroundTask(storage.close_file_stream, file_stream),
        )


@router.post(
    "/files/upload/avatar/{id}", response_model=SuccessResponse[AvatarResponse],
)
async def upload_avatar(
    user_id: Annotated[int, PathParam(alias="id")],
    request: Request,
    ctx: Ctx,
    avatar: Annotated[UploadFile | None, FastAPIFile()] = None,
    avatar_base64: Annotated[str | None, Form()] = None,
) -> SuccessEnvelope[dict[str, object]]:
    """从 multipart 表单、Base64 表单或 JSON 请求体中上传头像."""
    avatar_text: str | None = None
    if avatar is not None:
        if not avatar.filename:
            message = "No avatar file"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=message,
            )
    else:
        avatar_text = (avatar_base64 or "").strip()
        if not avatar_text and request.headers.get("content-type", "").startswith(
            "application/json",
        ):
            try:
                raw: object = await request.json()
                if isinstance(raw, dict):
                    body = cast("dict[str, object]", raw)
                    avatar_text = str(body.get("avatar") or "").strip()
            except Exception:
                avatar_text = ""

        if not avatar_text:
            message = "No avatar data"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=message,
            )

    result = await run_owned(
        upload_avatar_owned,
        ctx.session_factory,
        ctx.user.id,
        user_id,
        upload=avatar,
        encoded=avatar_text,
    )
    # 为兼容旧版前端客户端保留历史 avatar 响应字段名。
    if result.get("avatar_url") and "avatar" not in result:
        result["avatar"] = result["avatar_url"]
    return success_payload(result)


@router.post(
    "/files/batch-delete",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def batch_delete_files(
    payload: BatchDeleteRequest,
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(require_workspace_role("editor"))],
) -> Response:
    """批量删除指定列表中所包含的文件与文件夹."""
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        items: list[BatchDeleteItemData] = [
            {"id": item.id, "is_folder": item.is_folder}
            for item in payload.items
        ]
        file_service.batch_delete_items(session, workspace.id, ctx.user.id, items)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/files/retry_embedding",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def retry_embedding(
    payload: RetryEmbeddingRequest,
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
) -> Response:
    """将索引失败的单个文件重新加入索引任务队列."""
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        file_obj = file_service.get_authorized_file(
            session, workspace.id, ctx.user.id, payload.file_id,
        )
        file_service.retry_embedding(session, file_obj.id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/files/rebuild_failed_indexes", response_model=SuccessResponse[RebuildResponse],
)
def rebuild_failed_indexes(
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
) -> SuccessEnvelope[dict[str, int]]:
    """将当前工作空间下所有索引失败的文件批量重新加入队列."""
    with ctx.session_factory() as session:
        assert_can_write_locked(session, workspace.id, ctx.user.id)
        count = file_service.rebuild_failed_indexes(session, workspace.id)
        return success_payload({"count": count})


@router.get(
    "/files/process_status", response_model=SuccessResponse[ProcessStatusResponse],
)
async def process_status(
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
) -> SuccessEnvelope[dict[str, int]]:
    """聚合统计当前工作空间下各类处理状态的文件数量分布."""
    return success_payload(
        await file_service.process_status(
            None,
            workspace.id,
            session_factory=ctx.session_factory,
            user_id=ctx.user.id,
        ),
    )


# 必须置于所有具体 /files/* 路由之后, 防止 {id} 路径捕获它们。
@router.get("/files/{id}", response_model=SuccessResponse[FileResponse])
def get_file(
    file_id: Annotated[int, PathParam(alias="id")],
    ctx: Ctx,
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
) -> SuccessEnvelope[FileDict]:
    """校验工作空间成员资格后返回单个文件的详细元数据."""
    with ctx.session_factory() as session:
        assert_member(session, workspace.id, ctx.user.id)
        file_obj = file_service.get_authorized_file(
            session, workspace.id, ctx.user.id, file_id,
        )
        return success_payload(file_obj.to_dict())
