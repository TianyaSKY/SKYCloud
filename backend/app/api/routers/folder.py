from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse

from app.api.dependencies import get_current_user
from app.api.schemas.folder import FolderCreateRequest, FolderUpdateRequest
from app.services import folder_service

router = APIRouter(tags=["folder"])


@router.post("/folder")
def create_folder(payload: FolderCreateRequest, current_user=Depends(get_current_user)):
    data = payload.model_dump()
    data["user_id"] = current_user.id
    folder = folder_service.create_folder(data)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=folder.to_dict())


@router.put("/folder/{id}")
def update_folder(
        id: int, payload: FolderUpdateRequest, current_user=Depends(get_current_user)
):
    folder_service.get_authorized_folder(current_user.id, current_user.role, id)
    folder = folder_service.update_folder(id, payload.model_dump(exclude_none=True))
    return folder.to_dict()


@router.delete("/folder/{id}")
def delete_folder(id: int, current_user=Depends(get_current_user)):
    folder_service.get_authorized_folder(current_user.id, current_user.role, id)
    folder_service.delete_folder(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/folder/root_id")
def get_root_folder_id(current_user=Depends(get_current_user)):
    return {
        "root_folder_id": folder_service.get_root_folder_id(current_user.id),
        "code": 200,
    }


@router.get("/folder/{id}")
def get_folder(id: int, current_user=Depends(get_current_user)):
    folder = folder_service.get_authorized_folder(current_user.id, current_user.role, id)
    return folder.to_dict()


@router.get("/folder/all")
def get_folders(current_user=Depends(get_current_user)):
    folders = folder_service.get_folders(current_user.id)
    return {
        "folders": folders,
        "code": 200,
    }


@router.post("/folder/organize")
def organize_user_files(current_user=Depends(get_current_user)):
    queued = folder_service.organize_files(current_user.id)
    message = (
        "已开始整理文件，请留意收件箱里的通知，整理完成后会通知您"
        if queued
        else "已有整理任务在排队或执行中，请勿重复提交"
    )
    return {
        "message": message,
        "queued": queued,
        "code": 200,
    }
