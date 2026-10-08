"""分享路由：创建/取消分享与匿名 token 访问。业务在 share_service。"""

from typing import cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.dependencies import get_current_user
from app.features.share.schemas import ShareCreateRequest, ShareResponse, ShareAcknowledgment
from app.infra.extensions import get_session_factory
from app.infra.storage import get_storage_client
from app.features.share import service as share_service

from app.api.contracts import SuccessResponse, success_payload

router = APIRouter(tags=["share"])


@router.post("/share", status_code=status.HTTP_201_CREATED, response_model=SuccessResponse[ShareResponse])
def create_share(
    payload: ShareCreateRequest,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """为文件创建分享链接（可设过期时间）。"""
    with session_factory() as session:
        share = share_service.create_share(
            session, current_user.id, payload.file_id, payload.expires_at
        )
        return success_payload(share.to_dict())


@router.get("/share/my", response_model=SuccessResponse[list[ShareResponse]])
def get_my_shares(
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """列出当前用户发出的分享。"""
    with session_factory() as session:
        return success_payload(share_service.get_my_shares(session, current_user.id))


@router.delete("/share/{share_id}", response_model=SuccessResponse[ShareAcknowledgment])
def cancel_share(
    share_id: int,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """取消本人创建的分享链接。"""
    with session_factory() as session:
        share_service.cancel_share_for_user(session, share_id, current_user.id)
        return success_payload({"message": "Share cancelled successfully"})


@router.get("/share/{token}", response_class=StreamingResponse)
def access_share(token: str, session_factory=Depends(get_session_factory)):
    """匿名凭 token 内联预览/下载文件（从 MinIO 流式读取）；过期或无效由 service 抛错。"""
    with session_factory() as session:
        file = share_service.resolve_shared_file(session, token)
        storage = get_storage_client()
        object_name = cast(str, file.file_path)
        file_stream = storage.get_file_stream(object_name)

        filename = cast(str, file.name) or "download"
        encoded_filename = quote(filename)

        return StreamingResponse(
            file_stream,
            media_type=cast(str | None, file.mime_type) or "application/octet-stream",
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
            },
            background=BackgroundTask(storage.close_file_stream, file_stream),
        )
