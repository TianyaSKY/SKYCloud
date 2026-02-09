import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse

from app.services import share_service
from app.dependencies import get_current_user
from app.schemas import ShareCreateRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["share"])


@router.post("/share")
def create_share(payload: ShareCreateRequest, current_user=Depends(get_current_user)):
    expires_at = None
    if payload.expires_at:
        try:
            expires_at = datetime.fromisoformat(payload.expires_at)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid date format"
            ) from exc

    try:
        share = share_service.create_share_link(
            current_user.id, payload.file_id, expires_at
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content=share.to_dict()
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.error(f"Create share error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.get("/share/my")
def get_my_shares(current_user=Depends(get_current_user)):
    try:
        return share_service.get_my_shares(current_user.id)
    except Exception as exc:
        logger.error(f"Get my shares error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.delete("/share/{share_id}")
def cancel_share(share_id: int, current_user=Depends(get_current_user)):
    try:
        success = share_service.cancel_share(share_id, current_user.id)
        if success:
            return {"message": "Share cancelled successfully"}
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share not found or permission denied",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Cancel share error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.get("/share/{token}")
def access_share(token: str):
    share = share_service.get_share_by_token(token)
    if not share:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Link invalid or expired"
        )

    file = share.file
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    abs_path = file.get_abs_path()
    if not os.path.exists(abs_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found on server"
        )

    return FileResponse(
        abs_path,
        filename=file.name,
        media_type=file.mime_type,
        content_disposition_type="inline",
    )
