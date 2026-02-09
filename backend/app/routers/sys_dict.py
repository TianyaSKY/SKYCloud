from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse

from app.dependencies import require_admin
from app.schemas import SysDictPayload
from app.services import sys_dict_service

router = APIRouter(tags=["sys_dict"])


@router.post("/sys_dicts")
def create_sys_dict(payload: SysDictPayload, current_user=Depends(require_admin)):
    sys_dict = sys_dict_service.create_sys_dict(payload.model_dump())
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=sys_dict.to_dict())


@router.get("/sys_dicts/{id}")
async def get_sys_dict(id: int, current_user=Depends(require_admin)):
    result = await sys_dict_service.get_sys_dict(id)
    return result


@router.put("/sys_dicts/{id}")
def update_sys_dict(
        id: int, payload: SysDictPayload, current_user=Depends(require_admin)
):
    sys_dict = sys_dict_service.update_sys_dict(id, payload.model_dump())
    return sys_dict.to_dict()


@router.delete("/sys_dicts/{id}")
def delete_sys_dict(id: int, current_user=Depends(require_admin)):
    sys_dict_service.delete_sys_dict(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sys_dicts")
async def get_sys_dicts(current_user=Depends(require_admin)):
    return await sys_dict_service.get_sys_dict_all()
