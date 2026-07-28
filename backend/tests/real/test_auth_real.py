"""认证服务真实测试：auth/service.py + auth/user_service.py + mcp/token_service.py。

无 Mock：使用真实 SQLite 会话、真实 JWT 签发/解析、真实 Redis 缓存。
"""

import datetime as _dt
from datetime import timedelta

import jwt
import pytest

from app.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ServiceOperationError,
)
from app.features.auth.service import (
    authenticate_user,
    decode_token,
    generate_mcp_token,
    generate_token,
    get_mcp_token,
    issue_mcp_token,
    login,
    refresh_mcp_token,
    register_user,
)
from app.features.auth.user_service import (
    change_password,
    create_user,
    delete_user,
    ensure_user_access,
    get_user,
    update_user,
)
from app.infra.datetime_utils import beijing_now
from app.infra.extensions import SECRET_KEY
from app.mcp.token_service import (
    create_mcp_token,
    ensure_user_mcp_token,
    get_active_mcp_token,
    get_active_record,
    get_user_mcp_token_payload,
    refresh_user_mcp_token,
)
from app.models.mcp_token import McpToken
from app.models.user import User


# ===========================================================================
# auth/service.py — generate_token
# ===========================================================================


class TestGenerateToken:
    """generate_token：签发短期会话 JWT。"""

    def test_generate_token_success(self, session, test_user):
        """正常流程：返回有效 JWT，sub 为用户 ID。"""
        token = generate_token(test_user.id)
        assert token is not None
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        assert payload["sub"] == str(test_user.id)
        assert "exp" in payload
        assert "iat" in payload

    def test_generate_token_expiry(self, session, test_user):
        """Token 有效期为 1 天。"""
        token = generate_token(test_user.id)
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        exp = _dt.datetime.fromtimestamp(payload["exp"], tz=_dt.timezone.utc)
        iat = _dt.datetime.fromtimestamp(payload["iat"], tz=_dt.timezone.utc)
        delta = exp - iat
        assert _dt.timedelta(days=0, hours=23) < delta < _dt.timedelta(days=1, hours=1)

    def test_generate_token_different_users(self, session, test_user, admin_user):
        """不同用户生成不同 Token。"""
        t1 = generate_token(test_user.id)
        t2 = generate_token(admin_user.id)
        assert t1 != t2


# ===========================================================================
# auth/service.py — generate_mcp_token
# ===========================================================================


class TestGenerateMcpToken:
    """generate_mcp_token：签发 MCP 长效 JWT。"""

    def test_default_expiry_365_days(self, session, test_user):
        """默认有效期 365 天。"""
        token = generate_mcp_token(test_user.id)
        assert token is not None
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        assert payload["type"] == "mcp"
        assert payload["sub"] == str(test_user.id)
        assert "jti" in payload
        exp = _dt.datetime.fromtimestamp(payload["exp"], tz=_dt.timezone.utc)
        iat = _dt.datetime.fromtimestamp(payload["iat"], tz=_dt.timezone.utc)
        delta = exp - iat
        assert _dt.timedelta(days=364) < delta < _dt.timedelta(days=366)

    def test_custom_expiry(self, session, test_user):
        """自定义过期时间。"""
        custom_exp = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30)
        token = generate_mcp_token(test_user.id, expires_at=custom_exp)
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        exp = _dt.datetime.fromtimestamp(payload["exp"], tz=_dt.timezone.utc)
        assert abs((exp - custom_exp).total_seconds()) < 2

    def test_unique_jti(self, session, test_user):
        """每次签发 jti 唯一。"""
        t1 = generate_mcp_token(test_user.id)
        t2 = generate_mcp_token(test_user.id)
        p1 = jwt.decode(t1, SECRET_KEY, algorithms=["HS256"])
        p2 = jwt.decode(t2, SECRET_KEY, algorithms=["HS256"])
        assert p1["jti"] != p2["jti"]


# ===========================================================================
# auth/service.py — decode_token
# ===========================================================================


class TestDecodeToken:
    """decode_token：解析 JWT 并校验 MCP Token 状态。"""

    def test_decode_normal_token(self, session, test_user):
        """正常会话 Token 解析成功。"""
        token = generate_token(test_user.id)
        result = decode_token(session, token)
        assert result == str(test_user.id)

    def test_decode_expired_token(self, session, test_user):
        """过期 Token 返回提示文案。"""
        payload = {
            "exp": _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1),
            "iat": _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1),
            "sub": str(test_user.id),
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        result = decode_token(session, token)
        assert "expired" in result.lower()

    def test_decode_invalid_token(self, session):
        """无效 Token 返回提示文案。"""
        result = decode_token(session, "invalid.token.here")
        assert "invalid" in result.lower()

    def test_decode_tampered_token(self, session, test_user):
        """篡改签名的 Token 返回无效提示。"""
        token = generate_token(test_user.id)
        tampered = token[:-5] + "XXXXX"
        result = decode_token(session, tampered)
        assert "invalid" in result.lower() or "expired" in result.lower()

    def test_decode_mcp_token_valid(self, session, test_user):
        """有效 MCP Token：数据库有对应活跃记录。"""
        # 先签发并入库
        record, raw = ensure_user_mcp_token(session, test_user.id)
        result = decode_token(session, raw)
        assert result == str(test_user.id)

    def test_decode_mcp_token_revoked(self, session, test_user):
        """已撤销 MCP Token 返回提示。"""
        record, raw = ensure_user_mcp_token(session, test_user.id)
        # 撤销
        record.revoked_at = beijing_now()
        session.commit()
        result = decode_token(session, raw)
        assert "revoked" in result.lower() or "expired" in result.lower()


# ===========================================================================
# auth/service.py — authenticate_user
# ===========================================================================


class TestAuthenticateUser:
    """authenticate_user：用户名密码校验。"""

    def test_success(self, session, test_user):
        """正确凭据返回 token、role、user_id。"""
        token, role, uid = authenticate_user(session, "testuser", "password123")
        assert token is not None
        assert role == "common"
        assert uid == test_user.id

    def test_wrong_password(self, session, test_user):
        """密码错误返回 None。"""
        token, role, uid = authenticate_user(session, "testuser", "wrongpass")
        assert token is None
        assert uid is None

    def test_nonexistent_user(self, session):
        """不存在的用户返回 None。"""
        token, role, uid = authenticate_user(session, "ghost", "pass")
        assert token is None
        assert uid is None

    def test_admin_role(self, session, admin_user):
        """管理员用户返回 admin 角色。"""
        token, role, uid = authenticate_user(session, "adminuser", "admin123")
        assert role == "admin"
        assert uid == admin_user.id


# ===========================================================================
# auth/service.py — login
# ===========================================================================


class TestLogin:
    """login：登录并懒初始化 MCP Token。"""

    def test_login_success(self, session, test_user):
        """正常登录返回 token/role/user_id。"""
        result = login(session, "testuser", "password123")
        assert "token" in result
        assert result["role"] == "common"
        assert result["user_id"] == test_user.id

    def test_login_empty_username(self, session):
        """空用户名抛 BusinessRuleError。"""
        with pytest.raises(BusinessRuleError, match="Missing"):
            login(session, "", "pass")

    def test_login_empty_password(self, session, test_user):
        """空密码抛 BusinessRuleError。"""
        with pytest.raises(BusinessRuleError, match="Missing"):
            login(session, "testuser", "")

    def test_login_wrong_credentials(self, session, test_user):
        """错误凭据抛 AuthenticationError。"""
        with pytest.raises(AuthenticationError):
            login(session, "testuser", "wrongpass")

    def test_login_creates_mcp_token(self, session, test_user):
        """登录成功后自动创建 MCP Token。"""
        login(session, "testuser", "password123")
        record = get_active_record(session, test_user.id)
        assert record is not None
        assert record.token_value is not None


# ===========================================================================
# auth/service.py — register_user
# ===========================================================================


class TestRegisterUser:
    """register_user：注册并自动配置 MCP Token。"""

    def test_register_success(self, session):
        """正常注册返回 User 对象。"""
        user = register_user(session, "newuser", "newpass123")
        assert user.id is not None
        assert user.username == "newuser"
        assert user.role == "common"

    def test_register_creates_workspace(self, session):
        """注册自动创建私人工作空间。"""
        from app.models.workspace import Workspace, WorkspaceMember

        user = register_user(session, "wsuser", "pass123")
        ws = session.query(Workspace).filter_by(owner_id=user.id).first()
        assert ws is not None
        assert "wsuser" in ws.name
        member = session.query(WorkspaceMember).filter_by(
            workspace_id=ws.id, user_id=user.id
        ).first()
        assert member is not None
        assert member.role == "admin"

    def test_register_creates_mcp_token(self, session):
        """注册自动配置 MCP Token。"""
        user = register_user(session, "mcpuser", "pass123")
        record = get_active_record(session, user.id)
        assert record is not None

    def test_register_empty_username(self, session):
        """空用户名抛 BusinessRuleError。"""
        with pytest.raises(BusinessRuleError, match="Missing"):
            register_user(session, "", "pass")

    def test_register_empty_password(self, session):
        """空密码抛 BusinessRuleError。"""
        with pytest.raises(BusinessRuleError, match="Missing"):
            register_user(session, "user1", "")

    def test_register_duplicate_username(self, session, test_user):
        """重复用户名抛 BusinessRuleError。"""
        with pytest.raises(BusinessRuleError, match="already exists"):
            register_user(session, "testuser", "anotherpass")

    def test_register_with_avatar(self, session):
        """注册时可指定头像。"""
        user = register_user(session, "avataruser", "pass123", avatar="http://img.png")
        assert user.avatar == "http://img.png"


# ===========================================================================
# auth/service.py — get_mcp_token / refresh_mcp_token / issue_mcp_token
# ===========================================================================


class TestMcpTokenEndpoints:
    """MCP Token 获取/刷新/兼容接口。"""

    def test_get_mcp_token_creates_if_absent(self, session, test_user):
        """获取时不存在则自动签发。"""
        result = get_mcp_token(session, test_user.id)
        assert "mcp_token" in result
        assert result["user_id"] == test_user.id
        assert result["expires_in_days"] == 365

    def test_get_mcp_token_returns_existing(self, session, test_user):
        """已有时复用现有 Token。"""
        r1 = get_mcp_token(session, test_user.id)
        r2 = get_mcp_token(session, test_user.id)
        assert r1["mcp_token"] == r2["mcp_token"]

    def test_refresh_mcp_token(self, session, test_user):
        """刷新后旧 Token 失效，新 Token 有效。"""
        r1 = get_mcp_token(session, test_user.id)
        old_token = r1["mcp_token"]
        r2 = refresh_mcp_token(session, test_user.id)
        new_token = r2["mcp_token"]
        assert new_token != old_token
        # 旧 Token 已撤销
        assert get_active_mcp_token(session, old_token) is None
        # 新 Token 有效
        assert get_active_mcp_token(session, new_token) is not None

    def test_issue_mcp_token_compat(self, session, test_user):
        """issue_mcp_token 兼容旧接口，等价于 refresh。"""
        result = issue_mcp_token(session, test_user.id)
        assert "mcp_token" in result
        assert result["user_id"] == test_user.id
        assert result["expires_in_days"] == 365
        assert "usage" in result


# ===========================================================================
# auth/user_service.py — create_user
# ===========================================================================


class TestCreateUser:
    """create_user：创建用户 + 私人空间 + 根文件夹。"""

    def test_create_with_password(self, session):
        """使用 password 字段创建。"""
        user = create_user(session, {"username": "u1", "password": "p1"})
        assert user.id is not None
        assert user.check_password("p1")

    def test_create_with_password_hash(self, session):
        """使用 password_hash 字段直接设置哈希。"""
        from werkzeug.security import generate_password_hash

        h = generate_password_hash("direct_hash")
        user = create_user(session, {"username": "u2", "password_hash": h})
        assert user.password_hash == h
        assert user.check_password("direct_hash")

    def test_create_with_avatar(self, session):
        """创建时指定头像。"""
        user = create_user(session, {"username": "u3", "password": "p", "avatar": "a.png"})
        assert user.avatar == "a.png"

    def test_create_without_avatar(self, session):
        """不指定头像时默认 None。"""
        user = create_user(session, {"username": "u4", "password": "p"})
        assert user.avatar is None

    def test_create_sets_common_role(self, session):
        """新用户默认 common 角色。"""
        user = create_user(session, {"username": "u5", "password": "p"})
        assert user.role == "common"


# ===========================================================================
# auth/user_service.py — get_user / update_user / delete_user
# ===========================================================================


class TestUserCRUD:
    """用户 CRUD 操作。"""

    @pytest.mark.asyncio
    async def test_get_user_success(self, session, test_user):
        """获取存在的用户。"""
        user = await get_user(session, test_user.id)
        assert user.username == "testuser"

    @pytest.mark.asyncio
    async def test_get_user_not_found(self, session):
        """获取不存在用户抛 ResourceNotFoundError。"""
        with pytest.raises(ResourceNotFoundError):
            await get_user(session, 99999)

    def test_update_user_username(self, session, test_user):
        """更新用户名。"""
        updated = update_user(session, test_user.id, {"username": "newname"})
        assert updated.username == "newname"

    def test_update_user_avatar(self, session, test_user):
        """更新头像。"""
        updated = update_user(session, test_user.id, {"avatar": "new.png"})
        assert updated.avatar == "new.png"

    def test_update_user_password(self, session, test_user):
        """更新密码。"""
        update_user(session, test_user.id, {"password": "newpass"})
        user = session.get(User, test_user.id)
        assert user.check_password("newpass")

    def test_update_user_not_found(self, session):
        """更新不存在用户抛异常。"""
        with pytest.raises(ResourceNotFoundError):
            update_user(session, 99999, {"username": "x"})

    def test_delete_user_success(self, session, test_user):
        """删除用户。"""
        uid = test_user.id
        delete_user(session, uid)
        assert session.get(User, uid) is None

    def test_delete_user_not_found(self, session):
        """删除不存在用户抛异常。"""
        with pytest.raises(ResourceNotFoundError):
            delete_user(session, 99999)


# ===========================================================================
# auth/user_service.py — change_password
# ===========================================================================


class TestChangePassword:
    """change_password：本人或 admin 可改密。"""

    def test_self_change_with_correct_old_password(self, session, test_user):
        """本人凭旧密码改密成功。"""
        change_password(session, test_user.id, "common", test_user.id, "password123", "newpass")
        user = session.get(User, test_user.id)
        assert user.check_password("newpass")

    def test_self_change_with_wrong_old_password(self, session, test_user):
        """本人旧密码错误抛 BusinessRuleError。"""
        with pytest.raises(BusinessRuleError, match="Old password"):
            change_password(session, test_user.id, "common", test_user.id, "wrongold", "newpass")

    def test_admin_change_without_old_password(self, session, test_user, admin_user):
        """admin 无需旧密码即可改密。"""
        change_password(session, admin_user.id, "admin", test_user.id, "", "adminset")
        user = session.get(User, test_user.id)
        assert user.check_password("adminset")

    def test_non_admin_cannot_change_others(self, session, test_user, admin_user):
        """非 admin 不能改他人密码。"""
        with pytest.raises(PermissionDeniedError):
            change_password(session, test_user.id, "common", admin_user.id, "x", "y")

    def test_change_password_user_not_found(self, session, admin_user):
        """目标用户不存在抛 ResourceNotFoundError。"""
        with pytest.raises(ResourceNotFoundError):
            change_password(session, admin_user.id, "admin", 99999, "", "new")


# ===========================================================================
# auth/user_service.py — ensure_user_access
# ===========================================================================


class TestEnsureUserAccess:
    """ensure_user_access：仅允许本人或 admin 操作。"""

    def test_self_access(self):
        """本人访问通过。"""
        ensure_user_access(1, "common", 1)  # 不抛异常

    def test_admin_access(self):
        """admin 访问他人通过。"""
        ensure_user_access(1, "admin", 2)  # 不抛异常

    def test_other_user_denied(self):
        """非本人非 admin 抛 PermissionDeniedError。"""
        with pytest.raises(PermissionDeniedError):
            ensure_user_access(1, "common", 2)


# ===========================================================================
# mcp/token_service.py — 完整生命周期
# ===========================================================================


class TestMcpTokenService:
    """MCP Token 生命周期管理。"""

    def test_ensure_creates_token(self, session, test_user):
        """首次调用创建 Token。"""
        record, raw = ensure_user_mcp_token(session, test_user.id)
        assert record.id is not None
        assert record.user_id == test_user.id
        assert raw is not None
        assert record.token_value == raw

    def test_ensure_reuses_existing(self, session, test_user):
        """已有有效 Token 时复用。"""
        r1, raw1 = ensure_user_mcp_token(session, test_user.id)
        r2, raw2 = ensure_user_mcp_token(session, test_user.id)
        assert r1.id == r2.id
        assert raw1 == raw2

    def test_ensure_refreshes_if_no_token_value(self, session, test_user):
        """历史记录缺少 token_value 时重新签发。"""
        record, _ = ensure_user_mcp_token(session, test_user.id)
        record.token_value = None
        session.commit()
        r2, raw2 = ensure_user_mcp_token(session, test_user.id)
        assert raw2 is not None
        assert r2.token_value == raw2

    def test_refresh_revokes_old(self, session, test_user):
        """刷新后旧记录被撤销。"""
        r1, raw1 = refresh_user_mcp_token(session, test_user.id)
        r2, raw2 = refresh_user_mcp_token(session, test_user.id)
        assert r1.id != r2.id
        # 旧记录已撤销
        old = session.get(McpToken, r1.id)
        assert old.revoked_at is not None

    def test_get_active_record_none(self, session, test_user):
        """无 Token 时返回 None。"""
        assert get_active_record(session, test_user.id) is None

    def test_get_active_record_multiple_keeps_latest(self, session, test_user):
        """多条活跃记录只保留最新。"""
        # 手动创建两条活跃记录
        now = beijing_now()
        exp = now + timedelta(days=365)
        t1 = McpToken(
            user_id=test_user.id, name="T1",
            token_hash="hash1", token_preview="p1",
            token_value="val1", expires_at=exp,
        )
        t2 = McpToken(
            user_id=test_user.id, name="T2",
            token_hash="hash2", token_preview="p2",
            token_value="val2", expires_at=exp,
        )
        session.add_all([t1, t2])
        session.commit()
        # t2 更新（created_at 更晚）
        active = get_active_record(session, test_user.id)
        assert active is not None
        # 只保留一条活跃
        actives = session.query(McpToken).filter_by(
            user_id=test_user.id
        ).filter(McpToken.revoked_at.is_(None)).all()
        assert len(actives) == 1

    def test_get_active_mcp_token_valid(self, session, test_user):
        """有效 Token 鉴权成功并更新 last_used_at。"""
        record, raw = ensure_user_mcp_token(session, test_user.id)
        result = get_active_mcp_token(session, raw)
        assert result is not None
        assert result.id == record.id
        assert result.last_used_at is not None

    def test_get_active_mcp_token_invalid(self, session):
        """无效 Token 返回 None。"""
        result = get_active_mcp_token(session, "nonexistent_token")
        assert result is None

    def test_get_active_mcp_token_expired(self, session, test_user):
        """过期 Token 返回 None。"""
        record, raw = ensure_user_mcp_token(session, test_user.id)
        record.expires_at = beijing_now() - timedelta(days=1)
        session.commit()
        result = get_active_mcp_token(session, raw)
        assert result is None

    def test_get_user_mcp_token_payload(self, session, test_user):
        """API 响应格式正确。"""
        payload = get_user_mcp_token_payload(session, test_user.id)
        assert "mcp_token" in payload
        assert "token" in payload
        assert payload["user_id"] == test_user.id
        assert payload["expires_in_days"] == 365
        assert "usage" in payload

    def test_create_mcp_token_compat(self, session, test_user):
        """兼容旧签名 create_mcp_token。"""
        exp = beijing_now() + timedelta(days=30)
        record = create_mcp_token(session, test_user.id, "raw_jwt", exp, "My Token")
        assert record.name == "My Token"
        assert record.token_value == "raw_jwt"
        # 之前的 Token 被撤销
        r2 = create_mcp_token(session, test_user.id, "raw_jwt_2", exp, None)
        assert r2.name == "MCP Token"  # None 回退默认名
        old = session.get(McpToken, record.id)
        assert old.revoked_at is not None

    def test_token_name_strips_whitespace(self, session, test_user):
        """Token 名称去除首尾空格。"""
        exp = beijing_now() + timedelta(days=30)
        record = create_mcp_token(session, test_user.id, "jwt", exp, "  Padded  ")
        assert record.name == "Padded"

    def test_token_name_empty_fallback(self, session, test_user):
        """空名称回退为 'MCP Token'。"""
        exp = beijing_now() + timedelta(days=30)
        record = create_mcp_token(session, test_user.id, "jwt", exp, "   ")
        assert record.name == "MCP Token"


class TestRegisterOuterException:
    """覆盖 auth/service.py L115-118：register_user 外层 except 分支。

    触发方式：利用 session autoflush=False 特性，先向 session 添加一个
    与目标同名的 pending User（未 flush），pre-check 查询不会触发 autoflush
    所以看不到该 pending 对象，返回 None。随后 create_user 内部 flush
    尝试 INSERT 两个同名 User，触发 UNIQUE 约束冲突 → IntegrityError。
    """

    def test_register_user_flush_integrity_error(self, session):
        """注册时 flush 触发唯一约束冲突，进入外层 except（L115-118）。"""
        from app.models.user import User
        from app.exceptions import BusinessRuleError
        from app.features.auth.service import register_user

        # 添加 pending User（不 flush），autoflush=False 下查询不会自动刷入
        phantom = User(username="ghost_user", role="common")
        phantom.set_password("x")
        session.add(phantom)

        # register_user 的 pre-check 查询看不到 pending 对象（autoflush=False）
        # 进入 create_user → flush 时两个 'ghost_user' 同时 INSERT → UNIQUE 冲突
        with pytest.raises(BusinessRuleError):
            register_user(session, "ghost_user", "password123")
