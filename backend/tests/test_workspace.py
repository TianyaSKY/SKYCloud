"""工作空间测试：permissions / service。"""

import pytest

from app.exceptions import BusinessRuleError, PermissionDeniedError, ResourceNotFoundError
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.folder import Folder
from app.models.inbox import Inbox
from app.features.workspace.permissions import (
    WorkspaceRole,
    ROLE_LEVEL,
    get_member_role,
    assert_member,
    assert_can_read,
    assert_can_write,
    assert_admin,
    check_role_level,
)


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------


class TestWorkspaceRole:
    def test_enum_values(self):
        assert WorkspaceRole.admin == "admin"
        assert WorkspaceRole.editor == "editor"
        assert WorkspaceRole.viewer == "viewer"

    def test_role_level_mapping(self):
        assert ROLE_LEVEL["viewer"] == 1
        assert ROLE_LEVEL["editor"] == 2
        assert ROLE_LEVEL["admin"] == 3


class TestCheckRoleLevel:
    def test_admin_passes_all(self):
        assert check_role_level("admin", "viewer") is True
        assert check_role_level("admin", "editor") is True
        assert check_role_level("admin", "admin") is True

    def test_editor_passes_editor_and_viewer(self):
        assert check_role_level("editor", "viewer") is True
        assert check_role_level("editor", "editor") is True
        assert check_role_level("editor", "admin") is False

    def test_viewer_only_passes_viewer(self):
        assert check_role_level("viewer", "viewer") is True
        assert check_role_level("viewer", "editor") is False
        assert check_role_level("viewer", "admin") is False

    def test_enum_input(self):
        assert check_role_level(WorkspaceRole.admin, "editor") is True

    def test_unknown_role(self):
        assert check_role_level("unknown", "viewer") is False


class TestGetMemberRole:
    def test_member_exists(self, session, test_workspace, test_user):
        role = get_member_role(session, test_workspace.id, test_user.id)
        assert role == WorkspaceRole.admin

    def test_non_member(self, session, test_workspace):
        role = get_member_role(session, test_workspace.id, 99999)
        assert role is None


class TestAssertMember:
    def test_member_passes(self, session, test_workspace, test_user):
        role = assert_member(session, test_workspace.id, test_user.id)
        assert role == WorkspaceRole.admin

    def test_non_member_raises(self, session, test_workspace):
        with pytest.raises(PermissionDeniedError):
            assert_member(session, test_workspace.id, 99999)


class TestAssertCanRead:
    def test_viewer_can_read(self, session, test_workspace, test_user):
        # 将角色改为 viewer
        member = session.query(WorkspaceMember).filter_by(
            workspace_id=test_workspace.id, user_id=test_user.id
        ).first()
        member.role = "viewer"
        session.flush()

        role = assert_can_read(session, test_workspace.id, test_user.id)
        assert role == WorkspaceRole.viewer


class TestAssertCanWrite:
    def test_editor_can_write(self, session, test_workspace, test_user):
        member = session.query(WorkspaceMember).filter_by(
            workspace_id=test_workspace.id, user_id=test_user.id
        ).first()
        member.role = "editor"
        session.flush()

        assert_can_write(session, test_workspace.id, test_user.id)  # 不抛异常

    def test_viewer_cannot_write(self, session, test_workspace, test_user):
        member = session.query(WorkspaceMember).filter_by(
            workspace_id=test_workspace.id, user_id=test_user.id
        ).first()
        member.role = "viewer"
        session.flush()

        with pytest.raises(PermissionDeniedError):
            assert_can_write(session, test_workspace.id, test_user.id)


class TestAssertAdmin:
    def test_admin_passes(self, session, test_workspace, test_user):
        assert_admin(session, test_workspace.id, test_user.id)

    def test_editor_fails(self, session, test_workspace, test_user):
        member = session.query(WorkspaceMember).filter_by(
            workspace_id=test_workspace.id, user_id=test_user.id
        ).first()
        member.role = "editor"
        session.flush()

        with pytest.raises(PermissionDeniedError):
            assert_admin(session, test_workspace.id, test_user.id)


# ---------------------------------------------------------------------------
# workspace service
# ---------------------------------------------------------------------------


class TestCreateWorkspace:
    def test_create_workspace(self, session, test_user):
        from app.features.workspace.service import create_workspace

        ws = create_workspace(session, test_user.id, "新空间", "描述")
        assert ws.id is not None
        assert ws.name == "新空间"
        assert ws.owner_id == test_user.id

        # 验证 admin 成员
        member = session.query(WorkspaceMember).filter_by(
            workspace_id=ws.id, user_id=test_user.id
        ).first()
        assert member.role == "admin"

        # 验证根文件夹
        root = session.query(Folder).filter_by(workspace_id=ws.id, parent_id=None).first()
        assert root is not None


class TestGetUserWorkspaces:
    def test_list_workspaces(self, session, test_workspace, test_user):
        from app.features.workspace.service import get_user_workspaces

        result = get_user_workspaces(session, test_user.id)
        assert len(result) >= 1
        assert result[0]["my_role"] == "admin"
        assert result[0]["member_count"] >= 1


class TestGetWorkspace:
    def test_existing(self, session, test_workspace):
        from app.features.workspace.service import get_workspace

        ws = get_workspace(session, test_workspace.id)
        assert ws.id == test_workspace.id

    def test_nonexistent(self, session):
        from app.features.workspace.service import get_workspace

        with pytest.raises(ResourceNotFoundError):
            get_workspace(session, 99999)


class TestGetWorkspaceDetail:
    def test_detail_includes_members(self, session, test_workspace, test_user):
        from app.features.workspace.service import get_workspace_detail

        detail = get_workspace_detail(session, test_workspace.id, test_user.id)
        assert detail["my_role"] == "admin"
        assert len(detail["members"]) >= 1
        assert detail["members"][0]["username"] == "testuser"


class TestUpdateWorkspace:
    def test_admin_updates(self, session, test_workspace, test_user):
        from app.features.workspace.service import update_workspace

        ws = update_workspace(session, test_workspace.id, test_user.id, "新名称", "新描述")
        assert ws.name == "新名称"
        assert ws.description == "新描述"

    def test_non_admin_denied(self, session, test_workspace, test_user):
        from app.features.workspace.service import update_workspace

        # 创建另一个用户（非成员）
        other = User(username="outsider", role="common")
        other.set_password("pass")
        session.add(other)
        session.flush()

        with pytest.raises(PermissionDeniedError):
            update_workspace(session, test_workspace.id, other.id, "hack", None)


class TestDeleteWorkspace:
    def test_owner_deletes(self, session, test_workspace, test_user):
        from app.features.workspace.service import delete_workspace

        ws_id = test_workspace.id
        # 先清理关联记录（SQLite 外键约束）
        session.query(Folder).filter_by(workspace_id=ws_id).delete()
        session.query(WorkspaceMember).filter_by(workspace_id=ws_id).delete()
        session.flush()

        delete_workspace(session, ws_id, test_user.id)
        assert session.get(Workspace, ws_id) is None

    def test_non_owner_denied(self, session, test_workspace, test_user):
        from app.features.workspace.service import delete_workspace

        other = User(username="other2", role="common")
        other.set_password("pass")
        session.add(other)
        session.flush()

        with pytest.raises(PermissionDeniedError):
            delete_workspace(session, test_workspace.id, other.id)


class TestInviteMember:
    def test_invite_success(self, session, test_workspace, test_user):
        from app.features.workspace.service import invite_member

        # 创建被邀请用户
        invitee = User(username="invitee", role="common")
        invitee.set_password("pass")
        session.add(invitee)
        session.flush()

        member = invite_member(
            session, test_workspace.id, test_user.id, "invitee", "editor"
        )
        assert member.role == "editor"
        assert member.user_id == invitee.id

        # 验证通知
        notification = session.query(Inbox).filter_by(user_id=invitee.id).first()
        assert notification is not None
        assert "邀请" in notification.title

    def test_invite_nonexistent_user(self, session, test_workspace, test_user):
        from app.features.workspace.service import invite_member

        with pytest.raises(ResourceNotFoundError):
            invite_member(session, test_workspace.id, test_user.id, "ghost", "viewer")

    def test_invite_existing_member(self, session, test_workspace, test_user):
        from app.features.workspace.service import invite_member

        with pytest.raises(BusinessRuleError):
            invite_member(session, test_workspace.id, test_user.id, "testuser", "editor")


class TestRemoveMember:
    def test_remove_success(self, session, test_workspace, test_user):
        from app.features.workspace.service import remove_member

        # 添加一个成员
        member_user = User(username="removeme", role="common")
        member_user.set_password("pass")
        session.add(member_user)
        session.flush()

        m = WorkspaceMember(workspace_id=test_workspace.id, user_id=member_user.id, role="viewer")
        session.add(m)
        session.flush()

        remove_member(session, test_workspace.id, test_user.id, member_user.id)
        remaining = session.query(WorkspaceMember).filter_by(
            workspace_id=test_workspace.id, user_id=member_user.id
        ).first()
        assert remaining is None

    def test_cannot_remove_owner(self, session, test_workspace, test_user):
        from app.features.workspace.service import remove_member

        with pytest.raises(BusinessRuleError):
            remove_member(session, test_workspace.id, test_user.id, test_user.id)


class TestUpdateMemberRole:
    def test_update_role(self, session, test_workspace, test_user):
        from app.features.workspace.service import update_member_role

        member_user = User(username="rolechange", role="common")
        member_user.set_password("pass")
        session.add(member_user)
        session.flush()

        m = WorkspaceMember(workspace_id=test_workspace.id, user_id=member_user.id, role="viewer")
        session.add(m)
        session.flush()

        updated = update_member_role(
            session, test_workspace.id, test_user.id, member_user.id, "editor"
        )
        assert updated.role == "editor"

    def test_cannot_change_owner_role(self, session, test_workspace, test_user):
        from app.features.workspace.service import update_member_role

        with pytest.raises(BusinessRuleError):
            update_member_role(
                session, test_workspace.id, test_user.id, test_user.id, "viewer"
            )


class TestListMembers:
    def test_list(self, session, test_workspace, test_user):
        from app.features.workspace.service import list_members

        members = list_members(session, test_workspace.id)
        assert len(members) >= 1
        assert members[0]["username"] == "testuser"
