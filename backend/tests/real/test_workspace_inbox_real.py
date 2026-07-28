"""工作空间 + 站内信服务真实测试：workspace/service.py + inbox/service.py。

无 Mock：使用真实 SQLite 会话、真实 Redis 缓存。
"""

import pytest

from app.exceptions import (
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.inbox.service import (
    create_inbox_message,
    delete_inbox_message,
    get_inbox_message,
    get_user_inbox,
    mark_all_as_read,
    mark_as_read,
)
from app.features.workspace.permissions import (
    WorkspaceRole,
    assert_admin,
    assert_can_read,
    assert_can_write,
    assert_member,
    check_role_level,
    get_member_role,
)
from app.features.workspace.service import (
    create_workspace,
    delete_workspace,
    get_user_workspaces,
    get_workspace,
    get_workspace_detail,
    invite_member,
    list_members,
    remove_member,
    update_member_role,
    update_workspace,
)
from app.models.folder import Folder
from app.models.inbox import Inbox
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


# ===========================================================================
# workspace/permissions.py
# ===========================================================================


class TestWorkspacePermissions:
    """工作空间权限校验。"""

    def test_get_member_role_admin(self, session, test_workspace, test_user):
        """owner 角色为 admin。"""
        role = get_member_role(session, test_workspace.id, test_user.id)
        assert role == WorkspaceRole.admin

    def test_get_member_role_non_member(self, session, test_workspace, admin_user):
        """非成员返回 None。"""
        role = get_member_role(session, test_workspace.id, admin_user.id)
        assert role is None

    def test_assert_member_success(self, session, test_workspace, test_user):
        """成员断言通过。"""
        role = assert_member(session, test_workspace.id, test_user.id)
        assert role == WorkspaceRole.admin

    def test_assert_member_non_member(self, session, test_workspace, admin_user):
        """非成员断言抛 PermissionDeniedError。"""
        with pytest.raises(PermissionDeniedError):
            assert_member(session, test_workspace.id, admin_user.id)

    def test_assert_can_read(self, session, test_workspace, test_user):
        """任何成员可读。"""
        role = assert_can_read(session, test_workspace.id, test_user.id)
        assert role is not None

    def test_assert_can_write_admin(self, session, test_workspace, test_user):
        """admin 可写。"""
        assert_can_write(session, test_workspace.id, test_user.id)

    def test_assert_can_write_viewer_denied(self, session, test_workspace, test_user):
        """viewer 不可写。"""
        # 添加 viewer 成员
        viewer = User(username="viewer1", role="common")
        viewer.set_password("p")
        session.add(viewer)
        session.flush()
        session.add(WorkspaceMember(workspace_id=test_workspace.id, user_id=viewer.id, role="viewer"))
        session.commit()
        with pytest.raises(PermissionDeniedError):
            assert_can_write(session, test_workspace.id, viewer.id)

    def test_assert_admin_success(self, session, test_workspace, test_user):
        """admin 断言通过。"""
        assert_admin(session, test_workspace.id, test_user.id)

    def test_assert_admin_non_admin(self, session, test_workspace):
        """editor 不是 admin，断言失败。"""
        editor = User(username="editor1", role="common")
        editor.set_password("p")
        session.add(editor)
        session.flush()
        session.add(WorkspaceMember(workspace_id=test_workspace.id, user_id=editor.id, role="editor"))
        session.commit()
        with pytest.raises(PermissionDeniedError):
            assert_admin(session, test_workspace.id, editor.id)

    def test_check_role_level(self):
        """角色级别比较。"""
        assert check_role_level("admin", "viewer") is True
        assert check_role_level("admin", "editor") is True
        assert check_role_level("admin", "admin") is True
        assert check_role_level("editor", "admin") is False
        assert check_role_level("viewer", "editor") is False
        assert check_role_level(WorkspaceRole.admin, "editor") is True
        assert check_role_level("unknown", "viewer") is False


# ===========================================================================
# workspace/service.py
# ===========================================================================


class TestWorkspaceService:
    """工作空间 CRUD 与成员管理。"""

    def test_create_workspace(self, session, test_user):
        """创建空间 + 根文件夹 + admin 成员。"""
        ws = create_workspace(session, test_user.id, "新空间", "描述")
        assert ws.id is not None
        assert ws.name == "新空间"
        assert ws.description == "描述"
        # 根文件夹
        root = session.query(Folder).filter_by(workspace_id=ws.id, parent_id=None).first()
        assert root is not None
        assert root.name == "/"
        # admin 成员
        member = session.query(WorkspaceMember).filter_by(workspace_id=ws.id, user_id=test_user.id).first()
        assert member.role == "admin"

    def test_get_user_workspaces(self, session, test_workspace, test_user):
        """列出用户加入的空间。"""
        result = get_user_workspaces(session, test_user.id)
        assert len(result) >= 1
        ws_data = result[0]
        assert ws_data["my_role"] == "admin"
        assert ws_data["member_count"] >= 1

    def test_get_user_workspaces_empty(self, session, admin_user):
        """未加入任何空间返回空列表。"""
        result = get_user_workspaces(session, admin_user.id)
        assert result == []

    def test_get_workspace_success(self, session, test_workspace):
        """获取存在的空间。"""
        ws = get_workspace(session, test_workspace.id)
        assert ws.id == test_workspace.id

    def test_get_workspace_not_found(self, session):
        """获取不存在空间抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            get_workspace(session, 99999)

    def test_get_workspace_detail(self, session, test_workspace, test_user):
        """详情含成员列表和当前用户角色。"""
        detail = get_workspace_detail(session, test_workspace.id, test_user.id)
        assert detail["my_role"] == "admin"
        assert "members" in detail
        assert len(detail["members"]) >= 1
        assert detail["members"][0]["username"] == "testuser"

    def test_update_workspace_admin(self, session, test_workspace, test_user):
        """admin 可更新空间信息。"""
        ws = update_workspace(session, test_workspace.id, test_user.id, "新名称", "新描述")
        assert ws.name == "新名称"
        assert ws.description == "新描述"

    def test_update_workspace_non_admin_denied(self, session, test_workspace):
        """非 admin 更新被拒绝。"""
        editor = User(username="ed", role="common")
        editor.set_password("p")
        session.add(editor)
        session.flush()
        session.add(WorkspaceMember(workspace_id=test_workspace.id, user_id=editor.id, role="editor"))
        session.commit()
        with pytest.raises(PermissionDeniedError):
            update_workspace(session, test_workspace.id, editor.id, "hack", None)

    def test_delete_workspace_owner(self, session, test_user):
        """owner 可删除空间（无子资源时）。"""
        ws = create_workspace(session, test_user.id, "待删除")
        ws_id = ws.id
        # 清理 FK 关联记录（成员 + 根文件夹），模拟无子资源场景
        session.query(WorkspaceMember).filter_by(workspace_id=ws_id).delete()
        session.query(Folder).filter_by(workspace_id=ws_id).delete()
        session.flush()
        delete_workspace(session, ws_id, test_user.id)
        assert session.get(Workspace, ws_id) is None

    def test_delete_workspace_non_owner_denied(self, session, test_workspace, test_user):
        """非 owner 删除被拒绝。"""
        other = User(username="other", role="common")
        other.set_password("p")
        session.add(other)
        session.flush()
        session.add(WorkspaceMember(workspace_id=test_workspace.id, user_id=other.id, role="admin"))
        session.commit()
        with pytest.raises(PermissionDeniedError):
            delete_workspace(session, test_workspace.id, other.id)

    def test_invite_member_success(self, session, test_workspace, test_user):
        """邀请成员 + 发送通知。"""
        target = User(username="invitee", role="common")
        target.set_password("p")
        session.add(target)
        session.commit()
        member = invite_member(session, test_workspace.id, test_user.id, "invitee", "editor")
        assert member.role == "editor"
        assert member.invited_by == test_user.id
        # 通知
        notif = session.query(Inbox).filter_by(user_id=target.id, type="workspace").first()
        assert notif is not None
        assert "邀请" in notif.title

    def test_invite_member_user_not_found(self, session, test_workspace, test_user):
        """邀请不存在用户抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            invite_member(session, test_workspace.id, test_user.id, "ghost_user", "editor")

    def test_invite_member_already_member(self, session, test_workspace, test_user):
        """邀请已有成员抛 BusinessRuleError。"""
        with pytest.raises(BusinessRuleError, match="已是"):
            invite_member(session, test_workspace.id, test_user.id, "testuser", "editor")

    def test_remove_member_success(self, session, test_workspace, test_user):
        """移除成员 + 发送通知。"""
        target = User(username="removeme", role="common")
        target.set_password("p")
        session.add(target)
        session.flush()
        session.add(WorkspaceMember(workspace_id=test_workspace.id, user_id=target.id, role="viewer"))
        session.commit()
        remove_member(session, test_workspace.id, test_user.id, target.id)
        member = session.query(WorkspaceMember).filter_by(
            workspace_id=test_workspace.id, user_id=target.id
        ).first()
        assert member is None
        notif = session.query(Inbox).filter_by(user_id=target.id, type="workspace").first()
        assert "移除" in notif.title

    def test_remove_member_owner_denied(self, session, test_workspace, test_user):
        """不可移除 owner。"""
        with pytest.raises(BusinessRuleError, match="所有者"):
            remove_member(session, test_workspace.id, test_user.id, test_user.id)

    def test_remove_member_not_member(self, session, test_workspace, test_user, admin_user):
        """移除非成员抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            remove_member(session, test_workspace.id, test_user.id, admin_user.id)

    def test_update_member_role_success(self, session, test_workspace, test_user):
        """变更成员角色。"""
        target = User(username="rolechange", role="common")
        target.set_password("p")
        session.add(target)
        session.flush()
        session.add(WorkspaceMember(workspace_id=test_workspace.id, user_id=target.id, role="viewer"))
        session.commit()
        member = update_member_role(session, test_workspace.id, test_user.id, target.id, "editor")
        assert member.role == "editor"

    def test_update_member_role_owner_denied(self, session, test_workspace, test_user):
        """不可变更 owner 角色。"""
        with pytest.raises(BusinessRuleError, match="所有者"):
            update_member_role(session, test_workspace.id, test_user.id, test_user.id, "viewer")

    def test_update_member_role_not_member(self, session, test_workspace, test_user, admin_user):
        """变更非成员角色抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            update_member_role(session, test_workspace.id, test_user.id, admin_user.id, "editor")

    def test_list_members(self, session, test_workspace, test_user):
        """列出空间成员。"""
        members = list_members(session, test_workspace.id)
        assert len(members) >= 1
        assert members[0]["username"] == "testuser"
        assert "avatar" in members[0]


# ===========================================================================
# inbox/service.py
# ===========================================================================


class TestInboxService:
    """站内信 CRUD。"""

    def test_create_inbox_message(self, session, test_user):
        """创建消息。"""
        msg = create_inbox_message(session, {
            "user_id": test_user.id,
            "title": "测试标题",
            "content": "测试内容",
            "type": "system",
        })
        assert msg.id is not None
        assert msg.is_read is False
        assert msg.is_deleted is False

    def test_create_inbox_message_default_type(self, session, test_user):
        """默认 type 为 system。"""
        msg = create_inbox_message(session, {
            "user_id": test_user.id,
            "title": "T",
            "content": "C",
        })
        assert msg.type == "system"

    def test_get_user_inbox_pagination(self, session, test_user):
        """分页拉取未删除消息。"""
        for i in range(5):
            create_inbox_message(session, {
                "user_id": test_user.id, "title": f"msg{i}", "content": f"c{i}"
            })
        result = get_user_inbox(session, test_user.id, page=1, per_page=3)
        assert result["total"] == 5
        assert len(result["items"]) == 3
        assert result["pages"] == 2
        assert result["page"] == 1

    def test_get_user_inbox_page2(self, session, test_user):
        """第二页。"""
        for i in range(5):
            create_inbox_message(session, {
                "user_id": test_user.id, "title": f"msg{i}", "content": f"c{i}"
            })
        result = get_user_inbox(session, test_user.id, page=2, per_page=3)
        assert len(result["items"]) == 2

    def test_get_user_inbox_excludes_deleted(self, session, test_user):
        """已删除消息不出现在列表。"""
        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "del", "content": "c"
        })
        delete_inbox_message(session, msg.id, test_user.id)
        result = get_user_inbox(session, test_user.id)
        assert result["total"] == 0

    def test_get_user_inbox_empty(self, session, test_user):
        """无消息时返回空。"""
        result = get_user_inbox(session, test_user.id)
        assert result["total"] == 0
        assert result["items"] == []
        assert result["pages"] == 0  # (0 + 20 - 1) // 20 = 0

    def test_get_inbox_message_success(self, session, test_user):
        """获取单条消息。"""
        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "T", "content": "C"
        })
        fetched = get_inbox_message(session, msg.id)
        assert fetched.title == "T"

    def test_get_inbox_message_not_found(self, session):
        """不存在抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            get_inbox_message(session, 99999)

    def test_mark_as_read(self, session, test_user):
        """标记已读。"""
        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "T", "content": "C"
        })
        result = mark_as_read(session, msg.id, test_user.id)
        assert result.is_read is True

    def test_mark_as_read_wrong_user(self, session, test_user, admin_user):
        """非本人标记抛 404。"""
        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "T", "content": "C"
        })
        with pytest.raises(ResourceNotFoundError):
            mark_as_read(session, msg.id, admin_user.id)

    def test_delete_inbox_message_soft(self, session, test_user):
        """软删除。"""
        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "T", "content": "C"
        })
        delete_inbox_message(session, msg.id, test_user.id)
        # 数据库中仍存在，但 is_deleted=True
        raw = session.get(Inbox, msg.id)
        assert raw.is_deleted is True

    def test_delete_inbox_message_not_found(self, session, test_user):
        """删除不存在消息抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            delete_inbox_message(session, 99999, test_user.id)

    def test_mark_all_as_read(self, session, test_user):
        """全部标记已读。"""
        for i in range(3):
            create_inbox_message(session, {
                "user_id": test_user.id, "title": f"m{i}", "content": "c"
            })
        mark_all_as_read(session, test_user.id)
        unread = session.query(Inbox).filter_by(user_id=test_user.id, is_read=False).count()
        assert unread == 0

    def test_mark_all_as_read_no_unread(self, session, test_user):
        """无未读时不报错。"""
        mark_all_as_read(session, test_user.id)  # 不抛异常
