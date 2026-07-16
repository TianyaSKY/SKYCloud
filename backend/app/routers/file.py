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
from fastapi.responses import FileResponse, JSONResponse

from app.dependencies import get_current_user
from app.exceptions import DomainError
from app.api.schemas.file import (
    BatchDeleteRequest,
    FilePreflightRequest,
    FileUpdateRequest,
    MultipartCompleteRequest,
    MultipartInitRequest,
    RetryEmbeddingRequest,
)
from app.services import file_service
from app.upload_adapter import Base64UploadAdapter, FastAPIUploadAdapter

router = APIRouter(tags=["file"])


@router.post("/files")
def create_file(
    current_user=Depends(get_current_user),
    file: UploadFile = FastAPIFile(...),
    parent_id: int | None = Form(default=None),
):
    # 未指定 parent_id 时，默认上传到用户的根文件夹
    try:
        new_file = file_service.create_uploaded_file(
            current_user.id, FastAPIUploadAdapter(file), parent_id
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content=new_file.to_dict()
        )
    except (HTTPException, DomainError):
        raise
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
    # 未指定 parent_id 时，默认上传到用户的根文件夹
    adapters = [FastAPIUploadAdapter(file) for file in (files or []) if file]
    try:
        new_files = file_service.create_uploaded_files(
            current_user.id, adapters, parent_id
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=[f.to_dict() for f in new_files],
        )
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.post("/files/preflight")
def preflight_file_upload(
    payload: FilePreflightRequest,
    current_user=Depends(get_current_user),
):
    try:
        return file_service.preflight_file_upload(
            current_user.id, payload.model_dump(exclude_none=True)
        )
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.post("/files/multipart/init")
def init_multipart_upload(
    payload: MultipartInitRequest,
    current_user=Depends(get_current_user),
):
    try:
        return file_service.init_multipart_upload(
            current_user.id, payload.model_dump(exclude_none=True)
        )
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.post("/files/multipart/chunk")
def upload_multipart_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = FastAPIFile(...),
    current_user=Depends(get_current_user),
):
    if not chunk.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No chunk provided"
        )

    try:
        return file_service.save_multipart_chunk(
            current_user.id,
            upload_id,
            chunk_index,
            FastAPIUploadAdapter(chunk),
        )
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.post("/files/multipart/complete")
def complete_multipart_upload(
    payload: MultipartCompleteRequest,
    current_user=Depends(get_current_user),
):
    try:
        new_file = file_service.complete_multipart_upload(
            current_user.id, payload.upload_id
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content=new_file.to_dict()
        )
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@router.get("/files/multipart/{upload_id}")
def get_multipart_upload_status(
    upload_id: str,
    current_user=Depends(get_current_user),
):
    return file_service.get_multipart_upload_status(current_user.id, upload_id)


@router.delete("/files/multipart/{upload_id}")
def abort_multipart_upload(
    upload_id: str,
    current_user=Depends(get_current_user),
):
    file_service.abort_multipart_upload(current_user.id, upload_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/files/list")
def list_files(
    current_user=Depends(get_current_user),
    parent_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    name: str | None = Query(default=None, max_length=255),
    sort_by: str = Query(default="created_at", min_length=1, max_length=50),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
):
    return file_service.get_files_and_folders(
        current_user.id, parent_id, page, page_size, name, sort_by, order
    )


@router.get("/files/search")
async def search_files(
    current_user=Depends(get_current_user),
    q: str = Query(default="", max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    type: str = Query(default="fuzzy", pattern="^(fuzzy|semantic)$"),
):
    if not q:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    return await file_service.search_files(current_user.id, q, page, page_size, type)


@router.put("/files/{id}")
def update_file(
    id: int, payload: FileUpdateRequest, current_user=Depends(get_current_user)
):
    file_service.get_authorized_file(current_user.id, current_user.role, id)
    file_obj = file_service.update_file(id, payload.model_dump(exclude_none=True))
    return file_obj.to_dict()


@router.delete("/files/{id}")
def delete_file(id: int, current_user=Depends(get_current_user)):
    file_service.get_authorized_file(current_user.id, current_user.role, id)
    file_service.delete_file(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/files/{id}/download")
def download_file(id: int, current_user=Depends(get_current_user)):
    file_obj = file_service.get_downloadable_file(current_user.id, current_user.role, id)
    abs_path = file_obj.get_abs_path()

    return FileResponse(
        abs_path,
        filename=cast(str | None, file_obj.name),
        media_type=cast(str | None, file_obj.mime_type),
    )


@router.post("/files/upload/avatar/{id}")
async def upload_avatar(
    id: int,
    request: Request,
    avatar: UploadFile | None = FastAPIFile(default=None),
    avatar_base64: str | None = Form(default=None),
    current_user=Depends(get_current_user),
):
    adapter = None
    if avatar is not None:
        if not avatar.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="No avatar file"
            )
        adapter = FastAPIUploadAdapter(avatar)
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
        adapter = Base64UploadAdapter(avatar_text)

    result = file_service.upload_avatar_for_user(
        current_user.id, current_user.role, id, adapter
    )
    if result.get("avatar_url") and "avatar" not in result:
        result["avatar"] = result["avatar_url"]
    return result


@router.post("/files/batch-delete")
def batch_delete_files(
    payload: BatchDeleteRequest, current_user=Depends(get_current_user)
):
    items = [item.model_dump() for item in payload.items]
    file_service.batch_delete_items(current_user.id, current_user.role, items)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/files/retry_embedding")
def retry_embedding(
    payload: RetryEmbeddingRequest, current_user=Depends(get_current_user)
):
    file_obj = file_service.get_authorized_file(
        current_user.id, current_user.role, payload.file_id
    )
    file_service.retry_embedding(cast(int, file_obj.id))
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
    file_obj = file_service.get_authorized_file(current_user.id, current_user.role, id)
    return file_obj.to_dict()
