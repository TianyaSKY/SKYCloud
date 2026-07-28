"""模型层测试：to_dict / from_cache / 密码哈希 / 属性方法。"""

from datetime import datetime, timedelta

from app.infra.datetime_utils import beijing_now
from app.models.user import User
from app.models.file import File
from app.models.folder import Folder
from app.models.workspace import Workspace, WorkspaceMember
from app.models.share import Share
from app.models.inbox import Inbox
from app.models.sys_dict import SysDict
from app.models.mcp_token import McpToken
from app.models.token_usage_log import TokenUsageLog


# ---------------------------------------------------------------------------
# User 模型
# ---------------------------------------------------------------------------


class TestUserModel:
    def test_set_and_check_password(self):
        user = User(username="alice", role="common")
        user.set_password("secret123")
        assert user.check_password("secret123")
        assert not user.check_password("wrong")

    def test_to_dict(self):
        user = User(
            id=1,
            username="bob",
            role="admin",
            avatar="http://example.com/a.png",
            created_at=datetime(2025, 1, 1, 12, 0, 0),
            total_prompt_tokens=100,
            total_completion_tokens=50,
            total_tokens=150,
            last_active_at=datetime(2025, 6, 1, 8, 0, 0),
        )
        d = user.to_dict()
        assert d["id"] == 1
        assert d["username"] == "bob"
        assert d["role"] == "admin"
        assert d["avatar"] == "http://example.com/a.png"
        assert d["total_prompt_tokens"] == 100
        assert d["total_completion_tokens"] == 50
        assert d["total_tokens"] == 150
        assert "2025-01-01" in d["created_at"]
        assert "2025-06-01" in d["last_active_at"]

    def test_to_dict_none_tokens_defaults_zero(self):
        user = User(id=2, username="x", role="common")
        user.password_hash = "hash"
        d = user.to_dict()
        assert d["total_prompt_tokens"] == 0
        assert d["total_completion_tokens"] == 0
        assert d["total_tokens"] == 0
        assert d["last_active_at"] is None

    def test_from_cache_roundtrip(self):
        user = User(
            id=5,
            username="cache_user",
            role="common",
            avatar=None,
            created_at=datetime(2025, 3, 15, 10, 30, 0),
            total_prompt_tokens=200,
            total_completion_tokens=100,
            total_tokens=300,
            last_active_at=None,
        )
        d = user.to_dict()
        restored = User.from_cache(d)
        assert restored.id == 5
        assert restored.username == "cache_user"
        assert restored.role == "common"
        assert restored.total_tokens == 300
        assert restored.created_at == datetime(2025, 3, 15, 10, 30, 0)
        assert restored.last_active_at is None

    def test_repr(self):
        user = User(username="repr_test")
        assert "repr_test" in repr(user)


# ---------------------------------------------------------------------------
# File 模型
# ---------------------------------------------------------------------------


class TestFileModel:
    def test_to_dict(self):
        f = File(
            id=10,
            name="report.pdf",
            status="success",
            description="年度报告",
            file_size=1024,
            mime_type="application/pdf",
            content_hash="abc123",
            workspace_id=1,
            uploader_id=2,
            parent_id=3,
            created_at=datetime(2025, 5, 1, 9, 0, 0),
        )
        d = f.to_dict()
        assert d["id"] == 10
        assert d["name"] == "report.pdf"
        assert d["status"] == "success"
        assert d["description"] == "年度报告"
        assert d["file_size"] == 1024
        assert d["mime_type"] == "application/pdf"
        assert d["content_hash"] == "abc123"
        assert d["workspace_id"] == 1
        assert d["uploader_id"] == 2
        assert d["parent_id"] == 3

    def test_from_cache_roundtrip(self):
        f = File(
            id=11,
            name="data.csv",
            status="pending",
            file_size=512,
            workspace_id=1,
            uploader_id=1,
            parent_id=None,
            created_at=datetime(2025, 4, 1, 12, 0, 0),
        )
        d = f.to_dict()
        restored = File.from_cache(d)
        assert restored.id == 11
        assert restored.name == "data.csv"
        assert restored.status == "pending"
        assert restored.parent_id is None

    def test_get_abs_path(self):
        f = File(file_path="subdir/file.txt")
        path = f.get_abs_path()
        assert "subdir" in path
        assert "file.txt" in path


# ---------------------------------------------------------------------------
# Folder 模型
# ---------------------------------------------------------------------------


class TestFolderModel:
    def test_to_dict_root_folder(self):
        folder = Folder(id=1, name="/", workspace_id=1, parent_id=None)
        d = folder.to_dict()
        assert d["id"] == 1
        assert d["name"] == "/"
        assert d["parent_id"] is None
        assert d["path"] == "/"

    def test_to_dict_child_folder(self, session, test_workspace):
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        child = Folder(name="子目录", workspace_id=test_workspace.id, parent_id=root.id)
        session.add(child)
        session.flush()
        # 加载 parent 关系
        session.refresh(child)
        d = child.to_dict()
        assert d["name"] == "子目录"
        assert d["parent_id"] == root.id

    def test_from_cache(self):
        d = {
            "id": 99,
            "name": "缓存目录",
            "workspace_id": 1,
            "parent_id": None,
            "created_at": "2025-01-01T00:00:00",
        }
        folder = Folder.from_cache(d)
        assert folder.id == 99
        assert folder.name == "缓存目录"


# ---------------------------------------------------------------------------
# Workspace / WorkspaceMember 模型
# ---------------------------------------------------------------------------


class TestWorkspaceModel:
    def test_workspace_to_dict(self):
        ws = Workspace(
            id=1,
            name="团队空间",
            description="协作",
            owner_id=10,
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 2),
        )
        d = ws.to_dict()
        assert d["id"] == 1
        assert d["name"] == "团队空间"
        assert d["description"] == "协作"
        assert d["owner_id"] == 10

    def test_member_to_dict(self):
        m = WorkspaceMember(
            id=1,
            workspace_id=1,
            user_id=2,
            role="editor",
            invited_by=3,
            joined_at=datetime(2025, 3, 1),
        )
        d = m.to_dict()
        assert d["role"] == "editor"
        assert d["workspace_id"] == 1
        assert d["user_id"] == 2
        assert d["invited_by"] == 3

    def test_workspace_repr(self):
        ws = Workspace(id=5, name="repr_ws", owner_id=1)
        assert "repr_ws" in repr(ws)


# ---------------------------------------------------------------------------
# Share 模型
# ---------------------------------------------------------------------------


class TestShareModel:
    def test_to_dict(self):
        share = Share(
            id=1,
            token="abc-token",
            file_id=10,
            user_id=1,
            created_at=datetime(2025, 5, 1),
            expires_at=datetime(2025, 6, 1),
        )
        d = share.to_dict()
        assert d["token"] == "abc-token"
        assert d["file_id"] == 10
        assert d["link"] == "/api/share/abc-token"
        assert "2025-06-01" in d["expires_at"]

    def test_from_cache(self):
        d = {
            "id": 2,
            "token": "xyz",
            "file_id": 20,
            "created_at": "2025-01-01T00:00:00",
            "expires_at": None,
        }
        share = Share.from_cache(d)
        assert share.id == 2
        assert share.token == "xyz"
        assert share.expires_at is None


# ---------------------------------------------------------------------------
# Inbox 模型
# ---------------------------------------------------------------------------


class TestInboxModel:
    def test_to_dict(self):
        msg = Inbox(
            id=1,
            user_id=5,
            title="通知",
            content="您有一条新消息",
            is_read=False,
            type="system",
            created_at=datetime(2025, 4, 1),
        )
        d = msg.to_dict()
        assert d["title"] == "通知"
        assert d["content"] == "您有一条新消息"
        assert d["is_read"] is False
        assert d["type"] == "system"

    def test_from_cache(self):
        d = {
            "id": 3,
            "user_id": 1,
            "title": "t",
            "content": "c",
            "is_read": True,
            "type": "workspace",
            "created_at": "2025-02-01T10:00:00",
        }
        msg = Inbox.from_cache(d)
        assert msg.id == 3
        assert msg.is_read is True


# ---------------------------------------------------------------------------
# SysDict 模型
# ---------------------------------------------------------------------------


class TestSysDictModel:
    def test_to_dict(self):
        sd = SysDict(
            id=1, key="theme", value="dark", des="主题", enable=True,
            created_at=datetime(2025, 1, 1),
        )
        d = sd.to_dict()
        assert d["key"] == "theme"
        assert d["value"] == "dark"
        assert d["enable"] is True

    def test_from_cache(self):
        d = {"id": 2, "key": "lang", "value": "zh", "enable": True, "des": None, "created_at": None}
        sd = SysDict.from_cache(d)
        assert sd.key == "lang"
        assert sd.created_at is None


# ---------------------------------------------------------------------------
# McpToken 模型
# ---------------------------------------------------------------------------


class TestMcpTokenModel:
    def test_hash_token(self):
        h = McpToken.hash_token("my-secret-token")
        assert len(h) == 64  # SHA-256 hex
        assert h == McpToken.hash_token("my-secret-token")  # 确定性

    def test_preview_token_long(self):
        token = "a" * 50
        preview = McpToken.preview_token(token)
        assert preview.startswith("aaaaaaaa...")
        assert preview.endswith("...aaaaaaaa")

    def test_preview_token_short(self):
        token = "short"
        assert McpToken.preview_token(token) == "short"

    def test_is_revoked(self):
        t = McpToken(revoked_at=None)
        assert not t.is_revoked
        t.revoked_at = beijing_now()
        assert t.is_revoked

    def test_is_expired(self):
        t = McpToken(expires_at=beijing_now() - timedelta(days=1))
        assert t.is_expired
        t2 = McpToken(expires_at=beijing_now() + timedelta(days=1))
        assert not t2.is_expired

    def test_is_active(self):
        t = McpToken(
            expires_at=beijing_now() + timedelta(days=30),
            revoked_at=None,
        )
        assert t.is_active

    def test_to_dict(self):
        t = McpToken(
            id=1,
            user_id=1,
            name="My Token",
            token_preview="abc...xyz",
            token_value="full-jwt-value",
            created_at=datetime(2025, 1, 1),
            expires_at=datetime(2026, 1, 1),
            last_used_at=None,
            revoked_at=None,
        )
        d = t.to_dict()
        assert d["name"] == "My Token"
        assert d["is_revoked"] is False
        assert "mcp_token" not in d  # 默认不包含

        d_with_token = t.to_dict(include_token=True)
        assert d_with_token["mcp_token"] == "full-jwt-value"


# ---------------------------------------------------------------------------
# TokenUsageLog 模型
# ---------------------------------------------------------------------------


class TestTokenUsageLogModel:
    def test_to_dict(self):
        log = TokenUsageLog(
            id=1,
            user_id=1,
            action="chat",
            model_name="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            query_summary="测试问题",
            extra_info=None,
            created_at=datetime(2025, 5, 1),
        )
        d = log.to_dict()
        assert d["action"] == "chat"
        assert d["model_name"] == "gpt-4"
        assert d["prompt_tokens"] == 100
        assert d["total_tokens"] == 150
        assert d["query_summary"] == "测试问题"
