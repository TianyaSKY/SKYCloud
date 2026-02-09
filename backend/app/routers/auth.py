import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.services.auth_service import authenticate_user
from app.services.user_service import create_user
from app.schemas import LoginRequest, RegisterRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def login(payload: LoginRequest):
    if not payload.username or not payload.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing username or password",
        )

    token, role, user_id = authenticate_user(payload.username, payload.password)
    if token:
        return {
            "token": token,
            "message": "Login successful",
            "user": role,
            "user_id": user_id,
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
    )


@router.post("/auth/register")
def register(payload: RegisterRequest):
    if not payload.username or not payload.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing username or password",
        )

    try:
        user = create_user(payload.model_dump())
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"message": "User registered successfully", "user": user.to_dict()},
        )
    except Exception as exc:
        logger.error(f"Registration error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
