"""认证与用户路由：登录注册、MCP Token 管理、用户资料 CRUD。"""

from fastapi import APIRouter, Depends, Response, Request, status
import jwt

from app.api.contracts import SuccessResponse, success_payload
from app.api.dependencies import get_current_user, require_admin
from app.features.auth.schemas import (
    BrowserSessionResponse,
    LoginResponse,
    MessageResponse,
    RegistrationResponse,
    UserResponse,
    McpTokenResponse,
    McpTokenIssueResponse,
    LoginRequest,
    RegisterRequest,
    UserCreateRequest,
    UserPasswordUpdateRequest,
    UserUpdateRequest,
)
from app.infra.extensions import get_session_factory
from app.features.auth import service as auth_service
from app.features.auth import user_service
from app.features.auth import refresh_sessions
from app.infra.extensions import SECRET_KEY

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=SuccessResponse[LoginResponse])
def login(payload: LoginRequest, request: Request, response: Response, session_factory=Depends(get_session_factory)):
    """用户名密码登录，返回 JWT 与用户信息。"""
    with session_factory() as session:
        refresh_sessions.check_origin(request)
        result = auth_service.login(session, payload.username, payload.password)
        raw, result = refresh_sessions.create(session, jwt.decode(result['token'], SECRET_KEY, algorithms=['HS256']))
        refresh_sessions.revoke(refresh_sessions.credential(request))
        refresh_sessions.set_cookie(response, raw, refresh_sessions.REFRESH_TTL)
        return success_payload({"message": "Login successful", **result})


@router.post('/auth/refresh', response_model=SuccessResponse[BrowserSessionResponse])
def refresh(request: Request, response: Response, session_factory=Depends(get_session_factory)):
    with session_factory() as session:
        refresh_sessions.check_origin(request, required=True)
        raw, result, lifetime = refresh_sessions.refresh(session, refresh_sessions.credential(request))
        refresh_sessions.set_cookie(response, raw, lifetime)
        return success_payload(result)


@router.post('/auth/logout', response_model=SuccessResponse[MessageResponse])
def logout(request: Request, response: Response):
    refresh_sessions.check_origin(request, required=True)
    refresh_sessions.revoke(refresh_sessions.credential(request))
    refresh_sessions.revoke_access(request.headers.get('authorization', ''))
    refresh_sessions.clear_cookie(response)
    return success_payload({'message': 'Logged out'})


@router.post("/auth/register", response_model=SuccessResponse[RegistrationResponse], status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, session_factory=Depends(get_session_factory)):
    """注册新用户（默认普通角色）。"""
    with session_factory() as session:
        user = auth_service.register_user(
            session, payload.username, payload.password, payload.avatar
        )
        return success_payload({"message": "User registered successfully", "user": user.to_dict()})


@router.get("/auth/mcp-token", response_model=SuccessResponse[McpTokenResponse])
def get_mcp_token(
    response: Response,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """获取当前用户的 MCP Token 元数据，不返回完整凭据。"""
    with session_factory() as session:
        response.headers["Cache-Control"] = "no-store"
        return success_payload(auth_service.get_mcp_token(session, current_user.id))


@router.post("/auth/mcp-token/refresh", response_model=SuccessResponse[McpTokenIssueResponse])
def refresh_mcp_token(
    response: Response,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """刷新外部 MCP Token；OpenCode Runtime 使用独立短期 Token。"""
    with session_factory() as session:
        response.headers["Cache-Control"] = "no-store"
        return success_payload(auth_service.refresh_mcp_token(session, current_user.id))


# ---------------------------------------------------------------------------
# 用户资料 CRUD
# ---------------------------------------------------------------------------


@router.post("/users", response_model=SuccessResponse[UserResponse], status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    current_user=Depends(require_admin),
    session_factory=Depends(get_session_factory),
):
    """创建用户（公开注册入口之外的管理向创建）。"""
    with session_factory() as session:
        actor_role = user_service.authorize_account_mutation(session, current_user.id)
        user = user_service.create_user(session, payload.model_dump())
        return success_payload(user.to_dict())


@router.get("/users/{id}", response_model=SuccessResponse[UserResponse])
async def get_user(
    id: int,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """获取用户资料；仅本人或管理员。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)
    user = await user_service.get_user(None, id, session_factory=session_factory, actor_id=current_user.id)
    return success_payload(user.to_dict())


@router.put("/users/{id}", response_model=SuccessResponse[UserResponse])
def update_user(
    id: int,
    payload: UserUpdateRequest,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """更新用户资料；仅本人或管理员。"""
    with session_factory() as session:
        actor_role = user_service.authorize_account_mutation(session, current_user.id, id)
        user = user_service.update_user(session, id, payload.model_dump(exclude_none=True))
        return success_payload(user.to_dict())


@router.put("/users/{id}/password", response_model=SuccessResponse[MessageResponse])
def update_user_password(
    id: int,
    payload: UserPasswordUpdateRequest,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """修改密码：普通用户需旧密码，管理员可代改。"""
    with session_factory() as session:
        actor_role = user_service.authorize_account_mutation(session, current_user.id, id)

        user_service.change_password(
            session,
            current_user.id,
            actor_role,
            id,
            payload.old_password,
            payload.new_password,
        )
        return success_payload({"message": "Password updated successfully"})


@router.delete("/users/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_user(
    id: int,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """删除用户；仅本人或管理员。"""
    with session_factory() as session:
        actor_role = user_service.authorize_account_mutation(session, current_user.id, id)
        user_service.delete_user(session, id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
