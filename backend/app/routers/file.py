import os

from fastapi import (
    APIRouter,
    Depends,
    File as FastAPIFile,
    Form,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi import UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.dependencies import ensure_owner_or_admin, get_current_user
from app.schemas import (
    BatchDeleteRequest,
    FileUpdateRequest,
    RetryEmbeddingRequest,
    AvatarUploadRequest,
)
from app.services import file_service
from app.upload_adapter import FastAPIUploadAdapter, Base64UploadAdapter

router = APIRouter(tags=["file"])


def _ensure_file_access(current_user, file_id: int):
    file_obj = file_service.get_file(file_id)
    if current_user.role != "admin" and file_obj.uploader_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied"
        )
    return file_obj


@router.post("/files")
def create_file(
        current_user=Depends(get_current_user),
        file: UploadFile = FastAPIFile(...),
        parent_id: int | None = Form(default=None),
):
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No selected file"
        )

    data = {"uploader_id": current_user.id, "parent_id": parent_id}
    try:
        new_file = file_service.create_file(FastAPIUploadAdapter(file), data)
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content=new_file.to_dict()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.post("/files/batch")
def batch_upload_files(
        current_user=Depends(get_current_user),
        files: list[UploadFile] | None = FastAPIFile(default=None),
        parent_id: int | None = Form(default=None),
):
    valid_files = [f for f in (files or []) if f and f.filename]
    if not valid_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No selected files"
        )

    data = {"uploader_id": current_user.id, "parent_id": parent_id}
    adapters = [FastAPIUploadAdapter(f) for f in valid_files]
    try:
        new_files = file_service.batch_create_files(adapters, data)
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=[f.to_dict() for f in new_files],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.get("/files/list")
def list_files(
        current_user=Depends(get_current_user),
        parent_id: int | None = Query(default=None),
        page: int = Query(default=1),
        page_size: int = Query(default=10),
        name: str | None = Query(default=None),
        sort_by: str = Query(default="created_at"),
        order: str = Query(default="desc"),
):
    results = file_service.get_files_and_folders(
        current_user.id, parent_id, page, page_size, name, sort_by, order
    )
    return results


@router.get("/files/search")
async def search_files(
        current_user=Depends(get_current_user),
        q: str = Query(default=""),
        page: int = Query(default=1),
        page_size: int = Query(default=10),
        type: str = Query(default="fuzzy"),
):
    if not q:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    results = await file_service.search_files(current_user.id, q, page, page_size, type)
    return results


@router.put("/files/{id}")
def update_file(
        id: int, payload: FileUpdateRequest, current_user=Depends(get_current_user)
):
    _ensure_file_access(current_user, id)
    file_obj = file_service.update_file(id, payload.model_dump(exclude_none=True))
    return file_obj.to_dict()


@router.delete("/files/{id}")
def delete_file(id: int, current_user=Depends(get_current_user)):
    _ensure_file_access(current_user, id)
    file_service.delete_file(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/files/{id}/download")
def download_file(id: int, current_user=Depends(get_current_user)):
    file_obj = _ensure_file_access(current_user, id)
    abs_path = file_obj.get_abs_path()

    if not os.path.exists(abs_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found on server"
        )

    return FileResponse(abs_path, filename=file_obj.name, media_type=file_obj.mime_type)


@router.post("/files/upload/avatar/{id}")
def upload_avatar(
        id: int,
        payload: AvatarUploadRequest,
        current_user=Depends(get_current_user),
):
    ensure_owner_or_admin(current_user, id)
    if not payload.avatar:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No avatar data"
        )

    adapter = Base64UploadAdapter(payload.avatar)
    result = file_service.upload_avatar(adapter, id)
    if result.get("avatar_url") and "avatar" not in result:
        result["avatar"] = result["avatar_url"]
    return result


@router.post("/files/batch-delete")
def batch_delete_files(
        payload: BatchDeleteRequest, current_user=Depends(get_current_user)
):
    # 权限检查：确保用户有权限删除每个文件
    for item in payload.items:
        if not item.is_folder:
            _ensure_file_access(current_user, item.id)
        else:
            # 对于文件夹，检查用户是否是所有者或管理员
            from app.services import folder_service

            folder = folder_service.get_folder(item.id)
            if (
                    folder
                    and current_user.role != "admin"
                    and folder.user_id != current_user.id
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied"
                )
    items = [item.model_dump() for item in payload.items]
    file_service.batch_delete_items(items)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/files/retry_embedding")
def retry_embedding(
        payload: RetryEmbeddingRequest, current_user=Depends(get_current_user)
):
    file_obj = _ensure_file_access(current_user, payload.file_id)
    file_service.retry_embedding(file_obj.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/files/rebuild_failed_indexes")
def rebuild_failed_indexes(current_user=Depends(get_current_user)):
    count = file_service.rebuild_failed_indexes(current_user.id)
    return {"count": count}


@router.get("/files/process_status")
async def process_status(current_user=Depends(get_current_user)):
    return await file_service.process_status(current_user.id)


# 注意：此路由必须放在所有 /files/* 具体路由之后，避免 {id} 匹配到 "list"、"search" 等路径
@router.get("/files/{id}")
def get_file(id: int, current_user=Depends(get_current_user)):
    file_obj = _ensure_file_access(current_user, id)
    return file_obj.to_dict()
