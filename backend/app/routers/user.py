from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse

from app.dependencies import ensure_owner_or_admin, get_current_user
from app.models.user import User
from app.schemas import UserCreateRequest, UserPasswordUpdateRequest, UserUpdateRequest
from app.services import user_service

router = APIRouter(tags=["user"])


@router.post("/users")
def create_user(payload: UserCreateRequest):
    user = user_service.create_user(payload.model_dump())
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=user.to_dict())


@router.get("/users/{id}")
async def get_user(id: int, current_user=Depends(get_current_user)):
    ensure_owner_or_admin(current_user, id)
    user = await user_service.get_user(id)
    return user.to_dict()


@router.put("/users/{id}")
def update_user(
        id: int, payload: UserUpdateRequest, current_user=Depends(get_current_user)
):
    ensure_owner_or_admin(current_user, id)
    user = user_service.update_user(id, payload.model_dump(exclude_none=True))
    return user.to_dict()


@router.put("/users/{id}/password")
def update_user_password(
        id: int, payload: UserPasswordUpdateRequest, current_user=Depends(get_current_user)
):
    ensure_owner_or_admin(current_user, id)

    user = User.query.get(id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    if current_user.role != "admin" and not user.check_password(payload.old_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Old password is incorrect"
        )

    user_service.update_user(id, {"password": payload.new_password})
    return {"message": "Password updated successfully"}


@router.delete("/users/{id}")
def delete_user(id: int, current_user=Depends(get_current_user)):
    ensure_owner_or_admin(current_user, id)
    user_service.delete_user(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
