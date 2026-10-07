"""文件夹路由：CRUD 与一键整理入队。业务在 folder_service。"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.exceptions import ResourceNotFoundError
from app.api.contracts import SuccessResponse, success_payload
from app.api.dependencies import get_current_user, get_current_workspace, require_workspace_role
from app.features.folder.schemas import FolderCreateRequest, FolderUpdateRequest, FolderResponse, FolderListResponse, RootFolderResponse, OrganizeResponse
from app.infra.extensions import get_db
from app.models.workspace import Workspace
from app.features.folder import service as folder_service

router = APIRouter(tags=["folder"])


@router.post("/folder", response_model=SuccessResponse[FolderResponse], status_code=status.HTTP_201_CREATED)
def create_folder(
    payload: FolderCreateRequest,
    current_user=Depends(get_current_user),
    workspace: Workspace = Depends(require_workspace_role("editor")),
    session: Session = Depends(get_db),
):
    """在指定父目录下创建文件夹（归属当前工作空间）。"""
    data = payload.model_dump()
    data["workspace_id"] = workspace.id
    if data['parent_id'] is None:
        data['parent_id'] = folder_service.get_root_folder_id(session, workspace.id)
        if data['parent_id'] is None:
            raise ResourceNotFoundError('Workspace root folder not found')
    folder = folder_service.create_folder(session, data)
    return success_payload(folder.to_dict())


@router.put("/folder/{id}", response_model=SuccessResponse[FolderResponse])
def update_folder(
    id: int,
    payload: FolderUpdateRequest,
    current_user=Depends(get_current_user),
    workspace: Workspace = Depends(require_workspace_role("editor")),
    session: Session = Depends(get_db),
):
    """重命名或移动文件夹；先校验工作空间归属。"""
    folder_service.get_authorized_folder(session, workspace.id, current_user.id, id)
    folder = folder_service.update_folder(session, id, payload.model_dump(exclude_none=True))
    return success_payload(folder.to_dict())


@router.delete("/folder/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_folder(
    id: int,
    current_user=Depends(get_current_user),
    workspace: Workspace = Depends(require_workspace_role("editor")),
    session: Session = Depends(get_db),
):
    """删除文件夹（含级联策略由 service 决定）。"""
    folder_service.get_authorized_folder(session, workspace.id, current_user.id, id)
    folder_service.delete_folder(session, id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/folder/root_id", response_model=SuccessResponse[RootFolderResponse])
def get_root_folder_id(
    current_user=Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    """返回当前工作空间根文件夹 ID，供前端作为默认 parent。"""
    return success_payload({
        "root_folder_id": folder_service.get_root_folder_id(session, workspace.id),
    })


@router.get("/folder/all", response_model=SuccessResponse[FolderListResponse])
def get_folders(
    current_user=Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    """列出当前工作空间全部文件夹（扁平结构，供树构建）。"""
    folders = folder_service.get_folders(session, workspace.id)
    return success_payload({
        "folders": folders,
    })


@router.get("/folder/{id}", response_model=SuccessResponse[FolderResponse])
def get_folder(
    id: int,
    current_user=Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    """获取单个文件夹详情（含权限校验）。"""
    folder = folder_service.get_authorized_folder(
        session, workspace.id, current_user.id, id
    )
    return success_payload(folder.to_dict())


@router.post("/folder/organize", response_model=SuccessResponse[OrganizeResponse])
def organize_workspace_files(
    session: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    workspace: Workspace = Depends(require_workspace_role("editor")),
):
    """触发 AI 自动整理；已有任务在跑时拒绝重复入队。"""
    queued = folder_service.organize_files(session, workspace.id, current_user.id)
    message = (
        "已开始整理文件，请留意收件箱里的通知，整理完成后会通知您"
        if queued
        else "已有整理任务在排队或执行中，请勿重复提交"
    )
    return success_payload({
        "message": message,
        "queued": queued,
    })
