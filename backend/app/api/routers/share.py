from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse, JSONResponse

from app.api.dependencies import get_current_user
from app.api.schemas.share import ShareCreateRequest
from app.services import share_service

router = APIRouter(tags=["share"])


@router.post("/share")
def create_share(payload: ShareCreateRequest, current_user=Depends(get_current_user)):
    share = share_service.create_share(current_user.id, payload.file_id, payload.expires_at)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=share.to_dict())


@router.get("/share/my")
def get_my_shares(current_user=Depends(get_current_user)):
    return share_service.get_my_shares(current_user.id)


@router.delete("/share/{share_id}")
def cancel_share(share_id: int, current_user=Depends(get_current_user)):
    share_service.cancel_share_for_user(share_id, current_user.id)
    return {"message": "Share cancelled successfully"}


@router.get("/share/{token}")
def access_share(token: str):
    file = share_service.resolve_shared_file(token)
    abs_path = file.get_abs_path()

    return FileResponse(
        abs_path,
        filename=file.name,
        media_type=file.mime_type,
        content_disposition_type="inline",
    )
