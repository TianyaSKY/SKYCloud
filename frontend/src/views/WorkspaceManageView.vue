<template>
  <MainLayout active-menu="workspaces" title="管理工作空间">
    <div class="ws-manage">
      <!-- 操作栏 -->
      <div class="ws-toolbar">
        <a-button type="primary" @click="showCreateModal = true">
          <template #icon><icon-plus /></template>
          创建空间
        </a-button>
      </div>

      <!-- 空间列表 -->
      <a-spin :loading="wsStore.loading" style="width: 100%">
        <div class="ws-grid">
          <a-card
            v-for="ws in wsStore.workspaces"
            :key="ws.id"
            class="ws-card"
            :class="{ active: wsStore.currentWorkspace?.id === ws.id }"
            hoverable
            @click="wsStore.switchWorkspace(ws)"
          >
            <template #title>
              <div class="ws-card-title">
                <span>{{ ws.name }}</span>
                <a-tag v-if="ws.my_role === 'admin'" color="arcoblue" size="small">管理员</a-tag>
                <a-tag v-else-if="ws.my_role === 'editor'" color="green" size="small">编辑者</a-tag>
                <a-tag v-else color="gray" size="small">查看者</a-tag>
              </div>
            </template>
            <p class="ws-desc">{{ ws.description || '暂无描述' }}</p>
            <div class="ws-meta">
              <span>{{ ws.member_count ?? 0 }} 位成员</span>
              <a-tag :color="ws.status === 'running' ? 'green' : ws.status === 'error' ? 'red' : 'gray'" size="small">
                OpenCode {{ ws.status === 'running' ? '运行中' : ws.status === 'error' ? '异常' : '已停止' }}
              </a-tag>
            </div>
            <div class="ws-actions" @click.stop>
              <a-button v-if="ws.status === 'running' && ws.access_url" size="mini" type="primary" @click="openOpenCode(ws)">打开 OpenCode</a-button>
              <a-button v-if="ws.my_role !== 'viewer' && ws.status !== 'running'" size="mini" type="text" @click="handleStartOpenCode(ws)">启动 OpenCode</a-button>
              <a-button v-if="ws.my_role !== 'viewer' && ws.status === 'running'" size="mini" type="text" status="warning" @click="handleStopOpenCode(ws)">停止 OpenCode</a-button>
              <a-button size="mini" type="text" @click="openDetail(ws)">成员管理</a-button>
              <a-button
                v-if="ws.my_role === 'admin'"
                size="mini"
                type="text"
                status="danger"
                @click="handleDelete(ws)"
              >
                删除
              </a-button>
            </div>
          </a-card>
        </div>
      </a-spin>

      <!-- 创建空间弹窗 -->
      <a-modal v-model:visible="showCreateModal" title="创建工作空间" @ok="handleCreate" :ok-loading="creating">
        <a-form :model="createForm" layout="vertical">
          <a-form-item label="名称" required>
            <a-input v-model="createForm.name" placeholder="请输入空间名称" :max-length="128" />
          </a-form-item>
          <a-form-item label="描述">
            <a-textarea v-model="createForm.description" placeholder="可选描述" :max-length="512" />
          </a-form-item>
        </a-form>
      </a-modal>

      <!-- 成员管理抽屉 -->
      <a-drawer v-model:visible="showMemberDrawer" :title="`成员管理 - ${detailWs?.name ?? ''}`" :width="480">
        <div class="member-toolbar">
          <a-input v-model="inviteForm.username" placeholder="输入用户名" style="width: 180px" />
          <a-select v-model="inviteForm.role" style="width: 120px">
            <a-option value="editor">编辑者</a-option>
            <a-option value="viewer">查看者</a-option>
            <a-option value="admin">管理员</a-option>
          </a-select>
          <a-button type="primary" :loading="inviting" @click="handleInvite">邀请</a-button>
        </div>
        <a-list :bordered="false">
          <a-list-item v-for="m in members" :key="m.id">
            <a-list-item-meta :title="m.username || `用户${m.user_id}`">
              <template #avatar>
                <a-avatar :size="32">{{ (m.username || '?')[0] }}</a-avatar>
              </template>
              <template #description>
                <a-select
                  v-if="detailWs?.my_role === 'admin' && m.user_id !== detailWs?.owner_id"
                  :model-value="m.role"
                  size="mini"
                  style="width: 100px"
                  @change="(val: string) => handleRoleChange(m.user_id, val)"
                >
                  <a-option value="admin">管理员</a-option>
                  <a-option value="editor">编辑者</a-option>
                  <a-option value="viewer">查看者</a-option>
                </a-select>
                <a-tag v-else size="small">{{ m.role }}</a-tag>
              </template>
            </a-list-item-meta>
            <template #actions>
              <a-button
                v-if="detailWs?.my_role === 'admin' && m.user_id !== detailWs?.owner_id"
                size="mini"
                type="text"
                status="danger"
                @click="handleRemoveMember(m.user_id)"
              >
                移除
              </a-button>
            </template>
          </a-list-item>
        </a-list>
      </a-drawer>
    </div>
  </MainLayout>
</template>

<script lang="ts" setup>
import { reactive, ref } from 'vue'
import { Message, Modal } from '@arco-design/web-vue'
import { IconPlus } from '@arco-design/web-vue/es/icon'
import MainLayout from '@/components/MainLayout.vue'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  createWorkspace,
  deleteWorkspace,
  getWorkspace,
  inviteMember,
  listMembers,
  removeMember,
  updateMemberRole,
  startOpenCode,
  stopOpenCode,
  type MemberInfo,
  type WorkspaceInfo,
} from '@/api/workspace'

const wsStore = useWorkspaceStore()

// 创建空间
const showCreateModal = ref(false)
const creating = ref(false)
const createForm = reactive({ name: '', description: '' })

const handleCreate = async () => {
  if (!createForm.name.trim()) {
    Message.warning('请输入空间名称')
    return
  }
  creating.value = true
  try {
    await createWorkspace({ name: createForm.name.trim(), description: createForm.description || undefined })
    Message.success('创建成功')
    showCreateModal.value = false
    createForm.name = ''
    createForm.description = ''
    await wsStore.loadWorkspaces()
  } catch {
    // 拦截器已弹错误
  } finally {
    creating.value = false
  }
}

// 删除空间
const handleDelete = (ws: WorkspaceInfo) => {
  Modal.confirm({
    title: '删除工作空间',
    content: `确定删除「${ws.name}」？此操作不可恢复，空间内所有文件将被清除。`,
    okText: '删除',
    okButtonProps: { status: 'danger' },
    onOk: async () => {
      await deleteWorkspace(ws.id)
      Message.success('已删除')
      await wsStore.loadWorkspaces()
    },
  })
}

const refreshWorkspaces = () => wsStore.loadWorkspaces()

const handleStartOpenCode = async (ws: WorkspaceInfo) => {
  try {
    await startOpenCode(ws.id)
    Message.success('OpenCode 工作区已启动')
    await refreshWorkspaces()
  } catch {
    // 请求拦截器会显示错误
  }
}

const handleStopOpenCode = async (ws: WorkspaceInfo) => {
  try {
    await stopOpenCode(ws.id)
    Message.success('OpenCode 工作区已停止')
    await refreshWorkspaces()
  } catch {
    // 请求拦截器会显示错误
  }
}

const openOpenCode = (ws: WorkspaceInfo) => {
  if (ws.access_url) window.open(ws.access_url, `opencode-workspace-${ws.id}`)
}

// 成员管理
const showMemberDrawer = ref(false)
const detailWs = ref<WorkspaceInfo | null>(null)
const members = ref<MemberInfo[]>([])
const inviting = ref(false)
const inviteForm = reactive({ username: '', role: 'editor' })

const openDetail = async (ws: WorkspaceInfo) => {
  detailWs.value = ws
  showMemberDrawer.value = true
  try {
    const res = await listMembers(ws.id)
    members.value = res.members
  } catch {
    // ignore
  }
}

const handleInvite = async () => {
  if (!detailWs.value || !inviteForm.username.trim()) return
  inviting.value = true
  try {
    await inviteMember(detailWs.value.id, {
      username: inviteForm.username.trim(),
      role: inviteForm.role as 'admin' | 'editor' | 'viewer',
    })
    Message.success('邀请成功')
    inviteForm.username = ''
    const res = await listMembers(detailWs.value.id)
    members.value = res.members
  } catch {
    // ignore
  } finally {
    inviting.value = false
  }
}

const handleRoleChange = async (userId: number, role: string) => {
  if (!detailWs.value) return
  try {
    await updateMemberRole(detailWs.value.id, userId, role)
    Message.success('角色已更新')
    const res = await listMembers(detailWs.value.id)
    members.value = res.members
  } catch {
    // ignore
  }
}

const handleRemoveMember = (userId: number) => {
  if (!detailWs.value) return
  Modal.confirm({
    title: '移除成员',
    content: '确定移除该成员？',
    onOk: async () => {
      await removeMember(detailWs.value!.id, userId)
      Message.success('已移除')
      const res = await listMembers(detailWs.value!.id)
      members.value = res.members
    },
  })
}
</script>

<style scoped>
.ws-manage {
  max-width: 1200px;
}

.ws-toolbar {
  margin-bottom: 16px;
}

.ws-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}

.ws-card {
  cursor: pointer;
  transition: border-color 0.2s;
}

.ws-card.active {
  border-color: rgb(var(--arcoblue-6));
}

.ws-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ws-desc {
  color: var(--color-text-3);
  font-size: 13px;
  margin: 0 0 8px;
}

.ws-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--color-text-3);
}

.ws-actions {
  margin-top: 8px;
  display: flex;
  gap: 4px;
}

.member-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
</style>
