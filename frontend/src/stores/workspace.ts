import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { listWorkspaces, type WorkspaceInfo } from '../api/workspace'
import { useAuthStore } from './auth'
import { logger } from '../utils/logger'

const WS_KEY = 'current_workspace_id'

/**
 * 工作空间状态管理：当前选中空间、空间列表、角色权限。
 * 请求拦截器通过 currentWorkspace 注入 X-Workspace-Id header。
 */
export const useWorkspaceStore = defineStore('workspace', () => {
  const workspaces = ref<WorkspaceInfo[]>([])
  const currentWorkspace = ref<WorkspaceInfo | null>(null)
  const loading = ref(false)

  const myRole = computed(() => currentWorkspace.value?.my_role ?? 'viewer')
  const canWrite = computed(() => ['admin', 'editor'].includes(myRole.value))
  const isAdmin = computed(() => myRole.value === 'admin')

  const auth = useAuthStore()
  let generation = 0
  // Logout and replacement login invalidate both permissions and pending loads.
  watch(() => auth.sessionVersion, reset, { flush: 'sync' })

  /** Refresh authoritative membership, retaining the user's latest selection. */
  async function loadWorkspaces() {
    const current = ++generation
    loading.value = true
    try {
      const result = await listWorkspaces()
      if (current !== generation) return
      workspaces.value = result.workspaces
      let savedId: string | null = null
      try { savedId = localStorage.getItem(WS_KEY) } catch { /* Storage may be disabled. */ }
      const selectedId = currentWorkspace.value?.id ?? Number(savedId)
      const selected = workspaces.value.find(ws => ws.id === selectedId) ?? workspaces.value[0]
      if (selected) switchWorkspace(selected)
      else {
        currentWorkspace.value = null
        try { localStorage.removeItem(WS_KEY) } catch { /* Storage may be disabled. */ }
      }
    } catch (err) {
      if (current === generation) logger.warn('加载工作空间列表失败: {}', err)
    } finally {
      if (current === generation) loading.value = false
    }
  }

  /** 切换当前工作空间 */
  function switchWorkspace(ws: WorkspaceInfo) {
    currentWorkspace.value = ws
    try {
      localStorage.setItem(WS_KEY, String(ws.id))
    } catch {
      // ignore
    }
  }

  /** 清空状态（登出时调用） */
  function reset() {
    generation += 1
    loading.value = false
    workspaces.value = []
    currentWorkspace.value = null
    try {
      localStorage.removeItem(WS_KEY)
    } catch {
      // ignore
    }
  }

  return {
    workspaces,
    currentWorkspace,
    loading,
    myRole,
    canWrite,
    isAdmin,
    loadWorkspaces,
    switchWorkspace,
    reset,
  }
})
