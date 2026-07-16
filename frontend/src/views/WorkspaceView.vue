<template>
  <MainLayout active-menu="workspace" title="工作区">
    <div class="workspace-page">
      <!-- 顶部操作栏 -->
      <div class="workspace-toolbar">
        <a-button type="primary" @click="showCreateModal = true" :disabled="workspaces.length >= maxWorkspaces">
          <template #icon>
            <icon-plus />
          </template>
          新建工作区
        </a-button>
        <a-button @click="fetchWorkspaces" :loading="loading">
          <template #icon>
            <icon-refresh />
          </template>
          刷新
        </a-button>
        <span class="workspace-count">{{ workspaces.length }} / {{ maxWorkspaces }} 个工作区</span>
      </div>

      <!-- 工作区卡片列表 -->
      <div v-if="loading && workspaces.length === 0" class="workspace-loading">
        <a-spin size="32" />
        <span>加载中...</span>
      </div>

      <div v-else-if="workspaces.length === 0" class="workspace-empty">
        <icon-desktop style="font-size: 48px; color: var(--color-text-4);" />
        <p>还没有工作区</p>
        <p class="sub-text">创建一个工作区，开始使用 AI 编程助手</p>
        <a-button type="primary" @click="showCreateModal = true">
          <template #icon>
            <icon-plus />
          </template>
          创建第一个工作区
        </a-button>
      </div>

      <div v-else class="workspace-grid">
        <div
          v-for="ws in workspaces"
          :key="ws.id"
          class="workspace-card"
          :class="{ 'workspace-card--running': ws.status === 'running' }"
        >
          <div class="workspace-card__header">
            <div class="workspace-card__title">
              <icon-code-block class="workspace-card__icon" />
              <span>{{ ws.name }}</span>
            </div>
            <a-tag :color="statusColor(ws.status)" size="small">
              {{ statusLabel(ws.status) }}
            </a-tag>
          </div>

          <div class="workspace-card__info">
            <div class="workspace-card__detail">
              <icon-clock-circle />
              <span>创建于 {{ formatDate(ws.created_at) }}</span>
            </div>
            <div v-if="ws.container_id" class="workspace-card__detail">
              <icon-storage />
              <span>容器 {{ ws.container_id }}</span>
            </div>
            <div v-if="ws.error_message" class="workspace-card__error">
              <icon-exclamation-circle />
              <span>{{ ws.error_message }}</span>
            </div>
          </div>

          <div class="workspace-card__actions">
            <a-button
              v-if="ws.status === 'running'"
              type="primary"
              size="small"
              @click="openWorkspace(ws)"
            >
              <template #icon><icon-launch /></template>
              打开
            </a-button>
            <a-button
              v-if="ws.status === 'stopped' || ws.status === 'error'"
              type="primary"
              status="success"
              size="small"
              :loading="actionLoading[ws.id] === 'start'"
              @click="handleStart(ws)"
            >
              <template #icon><icon-play-arrow /></template>
              启动
            </a-button>
            <a-button
              v-if="ws.status === 'running'"
              type="outline"
              status="warning"
              size="small"
              :loading="actionLoading[ws.id] === 'stop'"
              @click="handleStop(ws)"
            >
              <template #icon><icon-pause /></template>
              停止
            </a-button>
            <a-button
              v-if="ws.status === 'running'"
              type="outline"
              size="small"
              :loading="actionLoading[ws.id] === 'restart'"
              @click="handleRestart(ws)"
            >
              <template #icon><icon-sync /></template>
              重启
            </a-button>
            <a-button
              v-if="ws.status === 'running'"
              type="outline"
              status="normal"
              size="small"
              :loading="actionLoading[ws.id] === 'mcp'"
              @click="handleSetupMcp(ws)"
            >
              <template #icon><icon-thunderbolt /></template>
              连接 MCP
            </a-button>
            <a-popconfirm
              content="确定删除该工作区？容器和数据将被永久删除。"
              @ok="handleDelete(ws)"
              type="warning"
            >
              <a-button
                type="outline"
                status="danger"
                size="small"
                :loading="actionLoading[ws.id] === 'delete'"
              >
                <template #icon><icon-delete /></template>
                删除
              </a-button>
            </a-popconfirm>
          </div>
        </div>
      </div>

      <!-- iframe 全屏工作区 -->
      <a-modal
        v-model:visible="iframeVisible"
        :title="activeWorkspace?.name || '工作区'"
        fullscreen
        :footer="false"
        :mask-closable="false"
        unmount-on-close
        class="workspace-modal"
      >
        <iframe
          v-if="activeWorkspace"
          :src="iframeSrc"
          class="workspace-iframe"
          allow="clipboard-write; clipboard-read"
        />
      </a-modal>

      <!-- 新建工作区弹窗 -->
      <a-modal
        v-model:visible="showCreateModal"
        title="新建工作区"
        @ok="handleCreate"
        :ok-loading="createLoading"
        ok-text="创建"
        cancel-text="取消"
      >
        <a-form :model="createForm" layout="vertical">
          <a-form-item label="工作区名称" field="name">
            <a-input
              v-model="createForm.name"
              placeholder="例如：我的项目"
              :max-length="120"
              allow-clear
            />
          </a-form-item>
        </a-form>
      </a-modal>

      <!-- MCP 连接成功弹窗 -->
      <a-modal
        v-model:visible="mcpResultVisible"
        title="MCP 连接配置成功"
        :footer="false"
        :width="520"
      >
        <div class="mcp-result">
          <div class="mcp-result__icon">
            <icon-check-circle-fill style="font-size: 48px; color: rgb(var(--green-6));" />
          </div>
          <p class="mcp-result__message">已自动将 MCP 连接配置写入工作区容器</p>
          <div class="mcp-result__details">
            <div class="mcp-result__item">
              <span class="mcp-result__label">MCP 地址</span>
              <span class="mcp-result__value">{{ mcpResult?.mcp_url }}</span>
            </div>
            <div class="mcp-result__item">
              <span class="mcp-result__label">配置路径</span>
              <span class="mcp-result__value">{{ mcpResult?.config_path }}</span>
            </div>
          </div>
          <a-alert type="info" style="margin-top: 16px;">
            MCP Token 已自动生成并配置。重启 opencode 后即可使用 SKYCLOUD MCP 工具。
          </a-alert>
        </div>
      </a-modal>
    </div>
  </MainLayout>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { Message } from '@arco-design/web-vue'
import {
  IconPlus,
  IconRefresh,
  IconDesktop,
  IconCodeBlock,
  IconClockCircle,
  IconStorage,
  IconExclamationCircle,
  IconLaunch,
  IconPlayArrow,
  IconPause,
  IconDelete,
  IconSync,
  IconThunderbolt,
  IconCheckCircleFill,
} from '@arco-design/web-vue/es/icon'
import MainLayout from '../components/MainLayout.vue'
import {
  listWorkspaces as apiList,
  createWorkspace as apiCreate,
  startWorkspace as apiStart,
  stopWorkspace as apiStop,
  deleteWorkspace as apiDelete,
  restartWorkspace as apiRestart,
  setupMcpConnection as apiSetupMcp,
  type WorkspaceInfo,
  type McpSetupResult,
} from '@/api/workspace'
import {useAuthStore} from '@/stores/auth'
import {createWorkspaceSchema} from '@/schemas/workspace'
import {formatDate} from '@/utils/format'
import {logger} from '@/utils/logger'

const workspaces = ref<WorkspaceInfo[]>([])
const loading = ref(false)
const maxWorkspaces = 3

// Create modal
const showCreateModal = ref(false)
const createLoading = ref(false)
const createForm = reactive({ name: '' })

// Action loading state per workspace
const actionLoading = ref<Record<number, string>>({})

// iframe
const iframeVisible = ref(false)
const activeWorkspace = ref<WorkspaceInfo | null>(null)
const iframeSrc = ref('')

// MCP result modal
const mcpResultVisible = ref(false)
const mcpResult = ref<McpSetupResult | null>(null)

// Auto-refresh timer
let refreshTimer: ReturnType<typeof setInterval> | null = null

const fetchWorkspaces = async () => {
  loading.value = true
  try {
    const res = await apiList()
    workspaces.value = res.workspaces
  } catch (err) {
    // 拦截器已弹唯一错误提示，此处仅记日志并吞异常，避免冒泡到全局 errorHandler 触发二次提示
    logger.warn('加载工作区列表失败 err={}', err)
  } finally {
    loading.value = false
  }
}

const handleCreate = async () => {
  const result = createWorkspaceSchema.safeParse({ name: createForm.name })
  if (!result.success) {
    Message.warning(result.error.issues[0]?.message ?? '请输入工作区名称')
    return
  }
  createLoading.value = true
  try {
    await apiCreate({ name: result.data.name })
    Message.success('工作区创建成功')
    showCreateModal.value = false
    createForm.name = ''
    await fetchWorkspaces()
  } catch (err) {
    // 拦截器已弹唯一错误提示
    logger.warn('创建工作区失败 name={} err={}', result.data.name, err)
  } finally {
    createLoading.value = false
  }
}

const handleStart = async (ws: WorkspaceInfo) => {
  actionLoading.value[ws.id] = 'start'
  try {
    await apiStart(ws.id)
    Message.success('工作区已启动')
    await fetchWorkspaces()
  } catch (err) {
    // 拦截器已弹唯一错误提示，吞异常避免冒泡到全局 errorHandler 触发二次提示
    logger.warn('工作区操作失败 id={} action=start err={}', ws.id, err)
  } finally {
    delete actionLoading.value[ws.id]
  }
}

const handleStop = async (ws: WorkspaceInfo) => {
  actionLoading.value[ws.id] = 'stop'
  try {
    await apiStop(ws.id)
    Message.success('工作区已停止')
    await fetchWorkspaces()
  } catch (err) {
    logger.warn('工作区操作失败 id={} action=stop err={}', ws.id, err)
  } finally {
    delete actionLoading.value[ws.id]
  }
}

const handleRestart = async (ws: WorkspaceInfo) => {
  actionLoading.value[ws.id] = 'restart'
  try {
    await apiRestart(ws.id)
    Message.success('工作区已重启')
    await fetchWorkspaces()
  } catch (err) {
    logger.warn('工作区操作失败 id={} action=restart err={}', ws.id, err)
  } finally {
    delete actionLoading.value[ws.id]
  }
}

const handleSetupMcp = async (ws: WorkspaceInfo) => {
  actionLoading.value[ws.id] = 'mcp'
  try {
    mcpResult.value = await apiSetupMcp(ws.id)
    mcpResultVisible.value = true
    Message.success('MCP 连接配置成功')
  } catch (err) {
    logger.warn('工作区操作失败 id={} action=setupMcp err={}', ws.id, err)
  } finally {
    delete actionLoading.value[ws.id]
  }
}

const handleDelete = async (ws: WorkspaceInfo) => {
  actionLoading.value[ws.id] = 'delete'
  try {
    await apiDelete(ws.id)
    Message.success('工作区已删除')
    await fetchWorkspaces()
  } catch (err) {
    logger.warn('工作区操作失败 id={} action=delete err={}', ws.id, err)
  } finally {
    delete actionLoading.value[ws.id]
  }
}

const auth = useAuthStore()

// 工作区直连地址 origin 白名单：仅允许与当前页面同主机（任意端口/协议），
// 避免后端返回的 access_url 被劫持后跳转到任意外部站点
const isAllowedWsOrigin = (url: string): boolean => {
  try {
    return new URL(url).hostname === window.location.hostname
  } catch {
    return false
  }
}

const openWorkspace = (ws: WorkspaceInfo) => {
  if (ws.access_url) {
    if (!isAllowedWsOrigin(ws.access_url)) {
      Message.error('工作区地址不在允许范围内，已阻止打开')
      return
    }
    // 在新标签页打开直连地址，获得完整 WebSocket 与原生 opencode UI 体验
    window.open(ws.access_url, `workspace-${ws.id}`)
  } else {
    // 兜底：通过反向代理在模态 iframe 中打开
    activeWorkspace.value = ws
    // 安全说明：iframe 无法附加 header，临时通过 query 传 token；
    // 后端 workspace proxy 接受 ?token= 并会下发 cookie，后续请求改走 cookie 鉴权。
    const token = auth.token || ''
    iframeSrc.value = `/api/workspace/${ws.id}/proxy/?token=${encodeURIComponent(token)}`
    iframeVisible.value = true
  }
}

const statusColor = (s: string) => {
  switch (s) {
    case 'running': return 'green'
    case 'stopped': return 'gray'
    case 'creating': return 'blue'
    case 'error': return 'red'
    default: return 'gray'
  }
}

const statusLabel = (s: string) => {
  switch (s) {
    case 'running': return '运行中'
    case 'stopped': return '已停止'
    case 'creating': return '创建中'
    case 'error': return '异常'
    default: return s
  }
}

// 标签页隐藏时不轮询，可见性切换回 visible 时立即刷新一次
const onVisibilityChange = () => {
  if (document.visibilityState === 'visible') {
    fetchWorkspaces()
  }
}

onMounted(() => {
  fetchWorkspaces()
  // 仅在页面可见时轮询；后台切换到可见时由 visibilitychange 监听立即拉取
  refreshTimer = setInterval(() => {
    if (document.visibilityState === 'visible') {
      fetchWorkspaces()
    }
  }, 15000)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<style scoped>
.workspace-page {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.workspace-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}

.workspace-count {
  margin-left: auto;
  color: var(--color-text-3);
  font-size: 13px;
}

.workspace-loading,
.workspace-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--color-text-3);
}

.workspace-empty .sub-text {
  font-size: 13px;
  color: var(--color-text-4);
  margin-bottom: 8px;
}

.workspace-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 16px;
}

.workspace-card {
  border: 1px solid var(--color-border-2);
  border-radius: 8px;
  padding: 20px;
  background: var(--color-bg-2);
  transition: box-shadow 0.2s, border-color 0.2s;
}

.workspace-card:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
}

.workspace-card--running {
  border-color: rgb(var(--green-6));
}

.workspace-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.workspace-card__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text-1);
}

.workspace-card__icon {
  font-size: 20px;
  color: rgb(var(--primary-6));
}

.workspace-card__info {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 16px;
}

.workspace-card__detail {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--color-text-3);
}

.workspace-card__error {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 13px;
  color: rgb(var(--red-6));
  word-break: break-all;
}

.workspace-card__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  padding-top: 12px;
  border-top: 1px solid var(--color-border-1);
}

/* iframe full-screen modal */
.workspace-modal :deep(.arco-modal-body) {
  padding: 0 !important;
  height: calc(100vh - 48px);
}

.workspace-iframe {
  width: 100%;
  height: 100%;
  border: none;
}

/* MCP result modal */
.mcp-result {
  text-align: center;
  padding: 8px 0;
}

.mcp-result__icon {
  margin-bottom: 16px;
}

.mcp-result__message {
  font-size: 15px;
  color: var(--color-text-1);
  margin-bottom: 20px;
}

.mcp-result__details {
  background: var(--color-fill-1);
  border-radius: 8px;
  padding: 16px;
  text-align: left;
}

.mcp-result__item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
}

.mcp-result__item + .mcp-result__item {
  border-top: 1px solid var(--color-border-1);
}

.mcp-result__label {
  font-size: 13px;
  color: var(--color-text-3);
  min-width: 72px;
  flex-shrink: 0;
}

.mcp-result__value {
  font-size: 13px;
  color: var(--color-text-1);
  font-family: 'Monaco', 'Menlo', monospace;
  word-break: break-all;
}
</style>
