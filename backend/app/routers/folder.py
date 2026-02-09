from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse

from app.services import folder_service
from app.dependencies import get_current_user
from app.schemas import FolderCreateRequest, FolderUpdateRequest

router = APIRouter(tags=["folder"])


def _ensure_folder_access(current_user, folder_id: int):
    folder = folder_service.get_folder(folder_id)
    if current_user.role != "admin" and folder.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied"
        )
    return folder


@router.post("/folder")
def create_folder(payload: FolderCreateRequest, current_user=Depends(get_current_user)):
    data = payload.model_dump()
    data["user_id"] = current_user.id
    folder = folder_service.create_folder(data)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=folder.to_dict())


@router.get("/folder/{id}")
def get_folder(id: int, current_user=Depends(get_current_user)):
    folder = _ensure_folder_access(current_user, id)
    return folder.to_dict()


@router.put("/folder/{id}")
def update_folder(
    id: int, payload: FolderUpdateRequest, current_user=Depends(get_current_user)
):
    _ensure_folder_access(current_user, id)
    folder = folder_service.update_folder(id, payload.model_dump(exclude_none=True))
    return folder.to_dict()


@router.delete("/folder/{id}")
def delete_folder(id: int, current_user=Depends(get_current_user)):
    _ensure_folder_access(current_user, id)
    folder_service.delete_folder(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/folder/root_id")
async def get_root_folder_id(current_user=Depends(get_current_user)):
    return {
        "root_folder_id": await folder_service.get_root_folder_id(current_user.id),
        "code": 200,
    }


@router.get("/folder/all")
async def get_folders(current_user=Depends(get_current_user)):
    folders = await folder_service.get_folders(current_user.id)
    return {
        "folders": folders,
        "code": 200,
    }


@router.post("/folder/organize")
def organize_user_files(current_user=Depends(get_current_user)):
    folder_service.organize_files(current_user.id)
    return {
        "message": "已开始整理文件，请留意收件箱里的通知，整理完成后会通知您",
        "code": 200,
    }
