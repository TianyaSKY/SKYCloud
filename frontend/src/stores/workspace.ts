import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { listWorkspaces, type WorkspaceInfo } from '../api/workspace'
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

  /** 加载用户所有工作空间，并恢复上次选中或默认选第一个 */
  async function loadWorkspaces() {
    loading.value = true
    try {
      const result = await listWorkspaces()
      workspaces.value = result.workspaces

      // 恢复上次选中的空间
      const savedId = localStorage.getItem(WS_KEY)
      if (savedId) {
        const found = workspaces.value.find((ws) => ws.id === Number(savedId))
        if (found) {
          currentWorkspace.value = found
          return
        }
      }
      // 默认选第一个
      const firstWorkspace = workspaces.value[0]
      if (firstWorkspace) {
        switchWorkspace(firstWorkspace)
      }
    } catch (err) {
      logger.warn('加载工作空间列表失败: {}', err)
    } finally {
      loading.value = false
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
