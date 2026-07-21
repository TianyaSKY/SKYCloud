"""分享路由：创建/取消分享与匿名 token 访问。业务在 share_service。"""

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.schemas.share import ShareCreateRequest
from app.extensions import get_db
from app.services import share_service

router = APIRouter(tags=["share"])


@router.post("/share")
def create_share(
    payload: ShareCreateRequest,
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """为文件创建分享链接（可设过期时间）。"""
    share = share_service.create_share(
        session, current_user.id, payload.file_id, payload.expires_at
    )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=share.to_dict())


@router.get("/share/my")
def get_my_shares(
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """列出当前用户发出的分享。"""
    return share_service.get_my_shares(session, current_user.id)


@router.delete("/share/{share_id}")
def cancel_share(
    share_id: int,
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """取消本人创建的分享链接。"""
    share_service.cancel_share_for_user(session, share_id, current_user.id)
    return {"message": "Share cancelled successfully"}


@router.get("/share/{token}")
def access_share(token: str, session: Session = Depends(get_db)):
    """匿名凭 token 内联预览/下载文件；过期或无效由 service 抛错。"""
    file = share_service.resolve_shared_file(session, token)
    abs_path = file.get_abs_path()

    return FileResponse(
        abs_path,
        filename=file.name,
        media_type=file.mime_type,
        content_disposition_type="inline",
    )
