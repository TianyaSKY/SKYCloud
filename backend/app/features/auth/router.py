"""认证与用户路由:登录注册、MCP Token 管理、用户资料 CRUD."""

from collections.abc import Mapping
from typing import Annotated, cast

import jwt
from fastapi import APIRouter, Depends, Path, Request, Response, status

from app.api.contracts import SuccessEnvelope, SuccessResponse, success_payload
from app.api.dependencies import get_current_user, require_admin
from app.features.auth import refresh_sessions, user_service
from app.features.auth import service as auth_service
from app.features.auth.schemas import (
    BrowserSessionResponse,
    LoginRequest,
    LoginResponse,
    McpTokenIssueResponse,
    McpTokenResponse,
    MessageResponse,
    RegisterRequest,
    RegistrationResponse,
    UserCreateRequest,
    UserPasswordUpdateRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.infra.extensions import SECRET_KEY, SessionFactory, get_session_factory
from app.models.user import User

router = APIRouter(tags=["auth"])

SessionFactoryDep = Annotated[SessionFactory, Depends(get_session_factory)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]


@router.post("/auth/login", response_model=SuccessResponse[LoginResponse])
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """用户名密码登录,返回 JWT 与用户信息."""
    with session_factory() as session:
        refresh_sessions.check_origin(request)
        result = auth_service.login(session, payload.username, payload.password)
        # authenticate_user minted this token, so it carries the required claims.
        claims = cast(
            "refresh_sessions.AccessClaims",
            jwt.decode(result["token"], SECRET_KEY, algorithms=["HS256"]),
        )
        raw, session_payload = refresh_sessions.create(session, claims)
        refresh_sessions.revoke(refresh_sessions.credential(request))
        refresh_sessions.set_cookie(response, raw, refresh_sessions.REFRESH_TTL)
        return success_payload({"message": "Login successful", **session_payload})


@router.post("/auth/refresh", response_model=SuccessResponse[BrowserSessionResponse])
def refresh(
    request: Request,
    response: Response,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """使用刷新凭据轮换浏览器会话."""
    with session_factory() as session:
        refresh_sessions.check_origin(request, required=True)
        raw, result, lifetime = refresh_sessions.refresh(
            session, refresh_sessions.credential(request),
        )
        refresh_sessions.set_cookie(response, raw, lifetime)
        return success_payload(result)


@router.post("/auth/logout", response_model=SuccessResponse[MessageResponse])
def logout(
    request: Request, response: Response,
) -> SuccessEnvelope[Mapping[str, object]]:
    """注销当前浏览器会话."""
    refresh_sessions.check_origin(request, required=True)
    refresh_sessions.revoke(refresh_sessions.credential(request))
    refresh_sessions.revoke_access(request.headers.get("authorization", ""))
    refresh_sessions.clear_cookie(response)
    return success_payload({"message": "Logged out"})


@router.post(
    "/auth/register",
    response_model=SuccessResponse[RegistrationResponse],
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: RegisterRequest, session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """注册新用户(默认普通角色)."""
    with session_factory() as session:
        user = auth_service.register_user(
            session, payload.username, payload.password, payload.avatar,
        )
        return success_payload(
            {"message": "User registered successfully", "user": user.to_dict()},
        )


@router.get("/auth/mcp-token", response_model=SuccessResponse[McpTokenResponse])
def get_mcp_token(
    response: Response,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """获取当前用户的 MCP Token 元数据,不返回完整凭据."""
    with session_factory() as session:
        response.headers["Cache-Control"] = "no-store"
        return success_payload(auth_service.get_mcp_token(session, current_user.id))


@router.post(
    "/auth/mcp-token/refresh", response_model=SuccessResponse[McpTokenIssueResponse],
)
def refresh_mcp_token(
    response: Response,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """刷新外部 MCP Token;OpenCode Runtime 使用独立短期 Token."""
    with session_factory() as session:
        response.headers["Cache-Control"] = "no-store"
        return success_payload(
            auth_service.refresh_mcp_token(session, current_user.id),
        )


# ---------------------------------------------------------------------------
# 用户资料 CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/users",
    response_model=SuccessResponse[UserResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_user(
    payload: UserCreateRequest,
    current_user: AdminUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """创建用户(公开注册入口之外的管理向创建)."""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id)
        user = user_service.create_user(
            session,
            {
                "username": payload.username,
                "password": payload.password,
                "avatar": payload.avatar,
            },
        )
        return success_payload(user.to_dict())


@router.get("/users/{id}", response_model=SuccessResponse[UserResponse])
async def get_user(
    user_id: Annotated[int, Path(alias="id")],
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """获取用户资料;仅本人或管理员."""
    user_service.ensure_user_access(current_user.id, current_user.role, user_id)
    user = await user_service.get_user(
        None, user_id, session_factory=session_factory, actor_id=current_user.id,
    )
    return success_payload(user.to_dict())


@router.put("/users/{id}", response_model=SuccessResponse[UserResponse])
def update_user(
    user_id: Annotated[int, Path(alias="id")],
    payload: UserUpdateRequest,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """更新用户资料;仅本人或管理员."""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id, user_id)
        profile: user_service.UpdateUserData = {}
        if payload.username is not None:
            profile["username"] = payload.username
        if payload.avatar is not None:
            profile["avatar"] = payload.avatar
        user = user_service.update_user(session, user_id, profile)
        return success_payload(user.to_dict())


@router.put("/users/{id}/password", response_model=SuccessResponse[MessageResponse])
def update_user_password(
    user_id: Annotated[int, Path(alias="id")],
    payload: UserPasswordUpdateRequest,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """修改密码:普通用户需旧密码,管理员可代改."""
    with session_factory() as session:
        actor_role = user_service.authorize_account_mutation(
            session, current_user.id, user_id,
        )
        user_service.change_password(
            session,
            user_service.ChangePasswordParams(
                actor_id=current_user.id,
                actor_role=actor_role,
                user_id=user_id,
                old_password=payload.old_password,
                new_password=payload.new_password,
            ),
        )
        return success_payload({"message": "Password updated successfully"})


@router.delete(
    "/users/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response,
)
def delete_user(
    user_id: Annotated[int, Path(alias="id")],
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> Response:
    """删除用户;仅本人或管理员."""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id, user_id)
        user_service.delete_user(session, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
