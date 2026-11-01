"""文件夹路由: CRUD 与一键整理入队. 业务在 folder_service."""

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from app.api.contracts import SuccessResponse, success_payload
from app.api.dependencies import Ctx, get_current_workspace, require_workspace_role
from app.features.folder import service as folder_service
from app.features.folder.organize.tasks import admit_task
from app.features.folder.schemas import (
    FolderCreateRequest,
    FolderListResponse,
    FolderResponse,
    FolderUpdateRequest,
    OrganizeResponse,
    RootFolderResponse,
)
from app.features.workspace.permissions import assert_can_write_locked, assert_member
from app.models.workspace import Workspace

router = APIRouter(tags=["folder"])

EditorWorkspace = Annotated[Workspace, Depends(require_workspace_role("editor"))]
MemberWorkspace = Annotated[Workspace, Depends(get_current_workspace)]


@router.post(
    "/folder",
    response_model=SuccessResponse[FolderResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_folder(
    payload: FolderCreateRequest,
    context: Ctx,
    workspace: EditorWorkspace,
) -> Mapping[str, object]:
    """在指定父目录下创建文件夹 (归属当前工作空间)."""
    with context.session_factory() as session:
        assert_can_write_locked(session, workspace.id, context.user.id)
        folder = folder_service.create_folder(
            session,
            {
                "workspace_id": workspace.id,
                "name": payload.name,
                "parent_id": payload.parent_id,
            },
        )
        return success_payload(folder.to_dict())


@router.put("/folder/{id}", response_model=SuccessResponse[FolderResponse])
def update_folder(
    folder_id: Annotated[int, Path(alias="id")],
    payload: FolderUpdateRequest,
    context: Ctx,
    workspace: EditorWorkspace,
) -> Mapping[str, object]:
    """重命名或移动文件夹; 先校验工作空间归属."""
    with context.session_factory() as session:
        assert_can_write_locked(session, workspace.id, context.user.id)
        folder_service.get_authorized_folder(
            session, workspace.id, context.user.id, folder_id,
        )
        update_data: folder_service.FolderUpdateData = {}
        if payload.name is not None:
            update_data["name"] = payload.name
        if payload.parent_id is not None:
            update_data["parent_id"] = payload.parent_id
        folder = folder_service.update_folder(session, folder_id, update_data)
        return success_payload(folder.to_dict())


@router.delete(
    "/folder/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_folder(
    folder_id: Annotated[int, Path(alias="id")],
    context: Ctx,
    workspace: EditorWorkspace,
) -> Response:
    """删除文件夹 (含级联策略由 service 决定)."""
    with context.session_factory() as session:
        assert_can_write_locked(session, workspace.id, context.user.id)
        folder_service.get_authorized_folder(
            session, workspace.id, context.user.id, folder_id,
        )
        folder_service.delete_folder(session, folder_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/folder/root_id", response_model=SuccessResponse[RootFolderResponse])
def get_root_folder_id(
    context: Ctx,
    workspace: MemberWorkspace,
) -> Mapping[str, object]:
    """返回当前工作空间根文件夹 ID, 供前端作为默认 parent."""
    with context.session_factory() as session:
        assert_member(session, workspace.id, context.user.id)
        return success_payload(
            {
                "root_folder_id": folder_service.get_root_folder_id(
                    session, workspace.id,
                ),
            },
        )


@router.get("/folder/all", response_model=SuccessResponse[FolderListResponse])
def get_folders(
    context: Ctx,
    workspace: MemberWorkspace,
) -> Mapping[str, object]:
    """列出当前工作空间全部文件夹 (扁平结构, 供树构建)."""
    with context.session_factory() as session:
        assert_member(session, workspace.id, context.user.id)
        folders = folder_service.get_folders(session, workspace.id)
        return success_payload({"folders": folders})


@router.get("/folder/{id}", response_model=SuccessResponse[FolderResponse])
def get_folder(
    folder_id: Annotated[int, Path(alias="id")],
    context: Ctx,
    workspace: MemberWorkspace,
) -> Mapping[str, object]:
    """获取单个文件夹详情 (含权限校验)."""
    with context.session_factory() as session:
        assert_member(session, workspace.id, context.user.id)
        folder = folder_service.get_authorized_folder(
            session, workspace.id, context.user.id, folder_id,
        )
        return success_payload(folder.to_dict())


@router.post("/folder/organize", response_model=SuccessResponse[OrganizeResponse])
def organize_workspace_files(
    context: Ctx,
    workspace: EditorWorkspace,
) -> Mapping[str, object]:
    """触发 AI 自动整理; 已有任务在跑时拒绝重复入队."""
    with context.session_factory() as session:
        assert_can_write_locked(session, workspace.id, context.user.id)
        queued = admit_task(session, workspace.id, context.user.id)
        message = (
            "已开始整理文件, 请留意收件箱里的通知, 整理完成后会通知您"
            if queued
            else "已有整理任务在排队或执行中, 请勿重复提交"
        )
        return success_payload({"message": message, "queued": queued})
