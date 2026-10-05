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
                SKYcode {{ ws.status === 'running' ? '运行中' : ws.status === 'error' ? '异常' : '已停止' }}
              </a-tag>
            </div>
            <div class="ws-actions" @click.stop>
              <a-button v-if="ws.status === 'running' && canWriteWorkspace(ws)" type="primary" @click="openSkycode(ws)">打开 SKYcode</a-button>
              <a-button v-if="canWriteWorkspace(ws) && ws.status !== 'running'" type="primary" @click="handleStartSkycode(ws)">启动 SKYcode</a-button>
              <a-button v-if="canWriteWorkspace(ws) && ws.status === 'running'" status="warning" @click="handleStopSkycode(ws)">停止 SKYcode</a-button>
              <a-button @click="openDetail(ws)">成员管理</a-button>
              <a-button
                v-if="ws.owner_id === auth.user.id"
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
      <RuntimeUiDialog v-model="runtimeVisible" :workspace="runtimeWorkspace" />
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
        <div v-if="detailWs?.my_role === 'admin'" class="member-toolbar">
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
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { Message, Modal } from '@arco-design/web-vue'
import { IconPlus } from '@arco-design/web-vue/es/icon'
import MainLayout from '@/components/MainLayout.vue'
import RuntimeUiDialog from '@/components/RuntimeUiDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import {
  createWorkspace,
  deleteWorkspace,
  inviteMember,
  listMembers,
  removeMember,
  updateMemberRole,
  startSkycode,
  stopSkycode,
  type MemberInfo,
  type WorkspaceInfo,
} from '@/api/workspace'

const wsStore = useWorkspaceStore()
const auth = useAuthStore()
const canWriteWorkspace = (ws: WorkspaceInfo) => ['admin', 'editor'].includes(ws.my_role ?? '')
const findWorkspace = (id: number) => wsStore.workspaces.find(ws => ws.id === id)
let generation = 0
let detailGeneration = 0
let memberRequest = 0
let disposed = false
const operations = new Set<string>()
const context = () => ({ generation, session: auth.sessionVersion })
const isCurrent = (value: ReturnType<typeof context>) => !disposed && value.generation === generation && value.session === auth.sessionVersion

const showCreateModal = ref(false)
const creating = ref(false)
const createForm = reactive({ name: '', description: '' })
const runtimeVisible = ref(false)
const runtimeWorkspace = ref<WorkspaceInfo | null>(null)
const showMemberDrawer = ref(false)
const detailId = ref<number | null>(null)
const detailWs = computed(() => detailId.value === null ? null : findWorkspace(detailId.value) ?? null)
const members = ref<MemberInfo[]>([])
const inviting = ref(false)
const inviteForm = reactive({ username: '', role: 'editor' })

function clearDetail() {
  detailGeneration += 1
  memberRequest += 1
  members.value = []
  inviting.value = false
  inviteForm.username = ''
}
watch(showMemberDrawer, visible => { if (!visible) { clearDetail(); detailId.value = null } }, { flush: 'sync' })
watch(() => auth.sessionVersion, () => {
  generation += 1
  clearDetail()
  detailId.value = null
  showMemberDrawer.value = false
  runtimeVisible.value = false
  runtimeWorkspace.value = null
  showCreateModal.value = false
  creating.value = false
  createForm.name = ''; createForm.description = ''
}, { flush: 'sync' })
watch(() => wsStore.workspaces, () => {
  if (detailId.value !== null && !detailWs.value) showMemberDrawer.value = false
  const current = runtimeWorkspace.value && findWorkspace(runtimeWorkspace.value.id)
  if (runtimeVisible.value && (!current || !canWriteWorkspace(current) || current.status !== 'running')) {
    runtimeVisible.value = false
    runtimeWorkspace.value = null
  }
}, { deep: true, flush: 'sync' })
onUnmounted(() => { disposed = true; generation += 1; clearDetail() })

async function runOperation(key: string, action: () => Promise<unknown>) {
  if (disposed || operations.has(key)) return
  operations.add(key)
  try { await action() } catch { /* The request interceptor reports current errors. */ }
  finally { operations.delete(key) }
}

const handleCreate = async () => {
  if (creating.value) return
  if (!createForm.name.trim()) { Message.warning('请输入空间名称'); return }
  const current = context()
  const data = { name: createForm.name.trim(), description: createForm.description || undefined }
  creating.value = true
  try {
    await createWorkspace(data)
    if (!isCurrent(current)) return
    Message.success('创建成功')
    showCreateModal.value = false
    createForm.name = ''; createForm.description = ''
    await wsStore.loadWorkspaces()
  } catch { /* Reported by interceptor. */ }
  finally { if (isCurrent(current)) creating.value = false }
}

const handleDelete = (ws: WorkspaceInfo) => {
  if (findWorkspace(ws.id)?.owner_id !== auth.user.id) return
  const current = context()
  Modal.confirm({
    title: '删除工作空间', content: `确定删除「${ws.name}」？此操作不可恢复，空间内所有文件将被清除。`,
    okText: '删除', okButtonProps: { status: 'danger' },
    onOk: () => runOperation(`delete:${ws.id}`, async () => {
      if (!isCurrent(current) || findWorkspace(ws.id)?.owner_id !== auth.user.id) return
      await deleteWorkspace(ws.id)
      if (!isCurrent(current)) return
      Message.success('已删除')
      await wsStore.loadWorkspaces()
    }),
  })
}

const changeRuntime = (ws: WorkspaceInfo, start: boolean) => {
  const current = context()
  return runOperation(`runtime:${ws.id}`, async () => {
    const workspace = findWorkspace(ws.id)
    if (!workspace || !canWriteWorkspace(workspace)) return
    await (start ? startSkycode(ws.id) : stopSkycode(ws.id))
    if (!isCurrent(current)) return
    Message.success(start ? 'SKYcode 工作区已启动' : 'SKYcode 工作区已停止')
    await wsStore.loadWorkspaces()
  })
}
const handleStartSkycode = (ws: WorkspaceInfo) => changeRuntime(ws, true)
const handleStopSkycode = (ws: WorkspaceInfo) => changeRuntime(ws, false)
const openSkycode = (ws: WorkspaceInfo) => {
  const current = findWorkspace(ws.id)
  if (!current || !canWriteWorkspace(current) || current.status !== 'running') return
  runtimeWorkspace.value = current
  runtimeVisible.value = true
}

const detailContext = () => ({ ...context(), detail: detailGeneration, id: detailId.value })
const isCurrentDetail = (value: ReturnType<typeof detailContext>) => isCurrent(value) && showMemberDrawer.value && value.detail === detailGeneration && value.id === detailId.value
const refreshMembers = async (current: ReturnType<typeof detailContext>) => {
  if (current.id === null || !isCurrentDetail(current)) return
  const request = ++memberRequest
  const res = await listMembers(current.id)
  if (request === memberRequest && isCurrentDetail(current)) members.value = res.members
}
const openDetail = async (ws: WorkspaceInfo) => {
  if (!findWorkspace(ws.id)) return
  clearDetail()
  detailId.value = ws.id
  showMemberDrawer.value = true
  try { await refreshMembers(detailContext()) } catch { /* Reported by interceptor. */ }
}
const canManageMember = (userId?: number) => detailWs.value?.my_role === 'admin' && (userId === undefined || userId !== detailWs.value.owner_id)

const handleInvite = async () => {
  if (inviting.value || !canManageMember() || !inviteForm.username.trim()) return
  const current = detailContext()
  if (current.id === null) return
  const data = { username: inviteForm.username.trim(), role: inviteForm.role as 'admin' | 'editor' | 'viewer' }
  inviting.value = true
  try {
    await inviteMember(current.id, data)
    if (!isCurrentDetail(current)) return
    Message.success('邀请成功')
    inviteForm.username = ''
    await refreshMembers(current)
    await wsStore.loadWorkspaces()
  } catch { /* Reported by interceptor. */ }
  finally { if (isCurrentDetail(current)) inviting.value = false }
}
const handleRoleChange = async (userId: number, role: string) => {
  if (!canManageMember(userId) || !['admin', 'editor', 'viewer'].includes(role)) return
  const current = detailContext()
  if (current.id === null) return
  await runOperation(`member:${current.id}:${userId}`, async () => {
    if (!isCurrentDetail(current) || !canManageMember(userId)) return
    await updateMemberRole(current.id!, userId, role)
    if (!isCurrentDetail(current)) return
    Message.success('角色已更新')
    if (userId === auth.user.id) {
      const workspace = findWorkspace(current.id!)
      if (workspace) workspace.my_role = role
      if (wsStore.currentWorkspace?.id === current.id) wsStore.currentWorkspace.my_role = role
    }
    await refreshMembers(current)
    await wsStore.loadWorkspaces()
  })
}
const handleRemoveMember = (userId: number) => {
  if (!canManageMember(userId)) return
  const current = detailContext()
  if (current.id === null) return
  Modal.confirm({
    title: '移除成员', content: '确定移除该成员？',
    onOk: () => runOperation(`member:${current.id}:${userId}`, async () => {
      if (!isCurrentDetail(current) || !canManageMember(userId)) return
      await removeMember(current.id!, userId)
      if (!isCurrentDetail(current)) return
      Message.success('已移除')
      if (userId === auth.user.id) {
        wsStore.workspaces = wsStore.workspaces.filter(ws => ws.id !== current.id)
        if (wsStore.currentWorkspace?.id === current.id) wsStore.currentWorkspace = null
        await wsStore.loadWorkspaces()
        return
      }
      await refreshMembers(current)
      await wsStore.loadWorkspaces()
    }),
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
  gap: 10px;
  font-size: 12px;
  color: var(--color-text-3);
}

.ws-actions {
  margin-top: 8px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.ws-actions :deep(.arco-btn) {
  justify-content: center;
  min-width: 0;
}

.member-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
</style>
