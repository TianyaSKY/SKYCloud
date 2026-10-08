"""系统字典路由：管理员维护键值配置。业务在 sys_dict_service。"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies import require_admin
from app.features.sys_dict.schemas import SysDictPayload, SysDictResponse
from app.infra.extensions import get_db
from app.features.sys_dict import service as sys_dict_service

from app.api.contracts import SuccessResponse, success_payload

router = APIRouter(tags=["sys_dict"])


@router.post("/sys_dicts", status_code=status.HTTP_201_CREATED, response_model=SuccessResponse[SysDictResponse])
def create_sys_dict(
    payload: SysDictPayload,
    current_user=Depends(require_admin),
    session: Session = Depends(get_db),
):
    """新增字典项（需管理员）。"""
    sys_dict = sys_dict_service.create_sys_dict(session, payload.model_dump())
    return success_payload(sys_dict.to_dict())


@router.get("/sys_dicts/{id}", response_model=SuccessResponse[SysDictResponse])
async def get_sys_dict(
    id: int,
    current_user=Depends(require_admin),
    session: Session = Depends(get_db),
):
    """按 ID 查询字典项。"""
    result = await sys_dict_service.get_sys_dict(session, id)
    return success_payload(result)


@router.put("/sys_dicts/{id}", response_model=SuccessResponse[SysDictResponse])
def update_sys_dict(
    id: int,
    payload: SysDictPayload,
    current_user=Depends(require_admin),
    session: Session = Depends(get_db),
):
    """更新字典项。"""
    sys_dict = sys_dict_service.update_sys_dict(session, id, payload.model_dump())
    return success_payload(sys_dict.to_dict())


@router.delete("/sys_dicts/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sys_dict(
    id: int,
    current_user=Depends(require_admin),
    session: Session = Depends(get_db),
):
    """删除字典项。"""
    sys_dict_service.delete_sys_dict(session, id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sys_dicts", response_model=SuccessResponse[list[SysDictResponse]])
async def get_sys_dicts(
    current_user=Depends(require_admin),
    session: Session = Depends(get_db),
):
    """列出全部字典项。"""
    return success_payload(await sys_dict_service.get_sys_dict_all(session))
