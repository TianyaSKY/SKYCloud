import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { Message } from '@arco-design/web-vue'

import {
  cancelAssistantRun,
  createAssistantConversation,
  getAssistantMessages,
  handoffAssistantConversation,
  listAssistantConversations,
  respondAssistantPermission,
  type AssistantConversation,
  type AssistantEvent,
  type AssistantMessageRecord,
  type AssistantMode,
} from '@/api/assistant'
import { sseRequest } from '@/utils/sseRequest'
import { BizError } from '@/api/request'
import { useWorkspaceStore } from '@/stores/workspace'

export interface AssistantToolCall {
  id: string
  name: string
  status: string
}

export interface AssistantPermission {
  permission_id: string
  title: string
  tool?: string
}

export interface AssistantMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  status: AssistantMessageRecord['status']
  keywords?: string
  sources?: Array<Record<string, unknown>>
  tools?: AssistantToolCall[]
  permission?: AssistantPermission
  diff?: Array<Record<string, unknown>>
  usage?: Record<string, unknown>
  local?: boolean
}

export const useAssistantStore = defineStore('assistant', () => {
  const currentMode = ref<AssistantMode>('fast')
  const workspaceId = ref<number | null>(null)
  const conversations = ref<AssistantConversation[]>([])
  const conversationIds = ref<Record<AssistantMode, number | null>>({ fast: null, expert: null })
  const messagesByConversation = ref<Record<number, AssistantMessage[]>>({})
  const activeRun = ref<{ id: number; messageId: number; permissionId?: string } | null>(null)
  const loading = ref(false)
  const initialized = ref(false)
  const expertAvailable = ref(true)

  let controller: AbortController | null = null
  let localMessageSequence = -1

  const currentConversationId = computed(() => conversationIds.value[currentMode.value])
  const messages = computed(() => {
    const id = currentConversationId.value
    return id ? messagesByConversation.value[id] || [] : []
  })
  const currentConversations = computed(() =>
    conversations.value
      .filter((conversation) => conversation.mode === currentMode.value)
      .sort((a, b) => (b.last_message_at || b.updated_at || '').localeCompare(a.last_message_at || a.updated_at || '')),
  )

  function mapMessage(record: AssistantMessageRecord): AssistantMessage {
    const metadata = record.metadata || {}
    return {
      id: record.id,
      role: record.role,
      content: record.content,
      status: record.status,
      keywords: typeof metadata.keywords === 'string' ? metadata.keywords : undefined,
      sources: Array.isArray(metadata.sources) ? (metadata.sources as Array<Record<string, unknown>>) : undefined,
      tools: Array.isArray(metadata.tool_calls) ? (metadata.tool_calls as AssistantToolCall[]) : undefined,
      diff: Array.isArray(metadata.diff) ? (metadata.diff as Array<Record<string, unknown>>) : undefined,
      usage: record.usage,
    }
  }

  function mergeConversation(conversation: AssistantConversation) {
    const index = conversations.value.findIndex((item) => item.id === conversation.id)
    if (index === -1) conversations.value.push(conversation)
    else conversations.value[index] = conversation
  }

  async function loadMode(mode: AssistantMode, createIfMissing = true) {
    if (!workspaceId.value) return
    try {
      const result = await listAssistantConversations(mode)
      result.conversations.forEach(mergeConversation)
      let conversation = result.conversations[0]
      if (!conversation && createIfMissing) {
        conversation = await createAssistantConversation(mode, mode === 'fast' ? '快速问答' : '专家任务')
        mergeConversation(conversation)
      }
      if (conversation) {
        conversationIds.value[mode] = conversation.id
        const history = await getAssistantMessages(conversation.id)
        messagesByConversation.value[conversation.id] = history.messages.map(mapMessage)
      }
      if (mode === 'expert') expertAvailable.value = true
    } catch (error) {
      if (mode === 'expert' && error instanceof BizError && error.status === 403) {
        expertAvailable.value = false
        return
      }
      throw error
    }
  }

  async function bindWorkspace(id: number | null) {
    if (workspaceId.value === id && initialized.value) return
    controller?.abort()
    controller = null
    workspaceId.value = id
    currentMode.value = 'fast'
    conversations.value = []
    conversationIds.value = { fast: null, expert: null }
    messagesByConversation.value = {}
    activeRun.value = null
    initialized.value = false
    if (!id) return
    await loadMode('fast')
    initialized.value = true
  }

  async function selectMode(mode: AssistantMode) {
    const workspace = useWorkspaceStore()
    if (loading.value) return
    if (mode === 'expert' && workspace.canWrite) expertAvailable.value = true
    if (mode === 'expert' && (!expertAvailable.value || !workspace.canWrite)) return
    currentMode.value = mode
    if (!conversationIds.value[mode]) await loadMode(mode)
  }

  async function selectConversation(id: number) {
    if (loading.value) return
    const conversation = conversations.value.find((item) => item.id === id)
    if (!conversation) return
    currentMode.value = conversation.mode
    conversationIds.value[conversation.mode] = id
    if (!messagesByConversation.value[id]) {
      const history = await getAssistantMessages(id)
      messagesByConversation.value[id] = history.messages.map(mapMessage)
    }
  }

  async function newConversation() {
    if (!workspaceId.value) return
    if (loading.value) return
    const workspace = useWorkspaceStore()
    if (currentMode.value === 'expert' && (!expertAvailable.value || !workspace.canWrite)) return
    const conversation = await createAssistantConversation(
      currentMode.value,
      currentMode.value === 'fast' ? '快速问答' : '专家任务',
    )
    mergeConversation(conversation)
    conversationIds.value[currentMode.value] = conversation.id
    messagesByConversation.value[conversation.id] = []
  }

  function appendTool(message: AssistantMessage, payload: Record<string, unknown>, type: string) {
    const id = String(payload.tool_call_id || payload.id || '')
    const tools = [...(message.tools || [])]
    const index = tools.findIndex((tool) => tool.id === id)
    const item: AssistantToolCall = {
      id,
      name: String(payload.tool || payload.title || '工作区工具'),
      status: String(payload.status || type.replace('tool_', '')),
    }
    if (index === -1) tools.push(item)
    else tools[index] = item
    message.tools = tools
  }

  function applyEvent(event: AssistantEvent, messageId: number) {
    const payload = event.payload || {}
    const message = messagesByConversation.value[currentConversationId.value || 0]?.find(
      (item) => item.id === messageId,
    )
    if (!message) return
    if (event.type === 'run_started' && event.message_id) {
      const list = messagesByConversation.value[currentConversationId.value || 0]
      const index = list?.findIndex((item) => item.id === messageId) ?? -1
      const existing = list && index >= 0 ? list[index] : undefined
      if (list && existing) {
        list[index] = { ...existing, id: event.message_id }
        activeRun.value = { id: event.run_id || 0, messageId: event.message_id }
      }
      return
    }
    if (event.type === 'token') {
      message.content += typeof payload.content === 'string' ? payload.content : ''
      message.status = 'streaming'
    } else if (event.type === 'keywords') {
      message.keywords = String(payload.content || '')
    } else if (event.type === 'sources') {
      message.sources = Array.isArray(payload.sources) ? (payload.sources as Array<Record<string, unknown>>) : []
    } else if (event.type.startsWith('tool_')) {
      appendTool(message, payload, event.type)
    } else if (event.type === 'permission_required') {
      message.permission = {
        permission_id: String(payload.permission_id || ''),
        title: String(payload.title || '专家请求执行高风险操作'),
        tool: payload.tool ? String(payload.tool) : undefined,
      }
      if (activeRun.value) activeRun.value.permissionId = message.permission.permission_id
      message.status = 'streaming'
    } else if (event.type === 'file_diff') {
      message.diff = Array.isArray(payload.files) ? (payload.files as Array<Record<string, unknown>>) : []
    } else if (event.type === 'usage') {
      message.usage = payload
    } else if (event.type === 'status') {
      message.status = 'streaming'
    } else if (event.type === 'done') {
      message.status = 'completed'
      if (activeRun.value?.id === event.run_id) activeRun.value = null
    } else if (event.type === 'cancelled') {
      message.status = 'cancelled'
      if (activeRun.value?.id === event.run_id) activeRun.value = null
    } else if (event.type === 'error') {
      message.status = 'failed'
      if (activeRun.value?.id === event.run_id) activeRun.value = null
    }
  }

  async function send(query: string) {
    if (!query.trim() || loading.value || !workspaceId.value) return
    if (!conversationIds.value[currentMode.value]) await loadMode(currentMode.value)
    const conversationId = conversationIds.value[currentMode.value]
    if (!conversationId) return
    const list = messagesByConversation.value[conversationId] || (messagesByConversation.value[conversationId] = [])
    const userMessage: AssistantMessage = {
      id: localMessageSequence--,
      role: 'user',
      content: query.trim(),
      status: 'completed',
      local: true,
    }
    const assistantMessage: AssistantMessage = {
      id: localMessageSequence--,
      role: 'assistant',
      content: '',
      status: 'pending',
      local: true,
    }
    list.push(userMessage, assistantMessage)
    loading.value = true
    controller?.abort()
    controller = new AbortController()
    const localId = assistantMessage.id
    let streamMessageId = localId
    try {
      await sseRequest(
        `/assistant/conversations/${conversationId}/messages/stream`,
        { query: query.trim() },
        {
          signal: controller.signal,
          onEvent: (event) => {
            applyEvent(event, streamMessageId)
            if (event.type === 'run_started' && event.message_id) streamMessageId = event.message_id
          },
        },
      )
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      const message = list.find((item) => item.id === streamMessageId)
      if (message) {
        message.status = 'failed'
        message.content = message.content || '抱歉，任务执行失败。'
      }
      if (!(error instanceof DOMException && error.name === 'AbortError')) Message.error((error as Error).message || '发送失败')
    } finally {
      loading.value = false
      controller = null
      if (activeRun.value?.messageId === streamMessageId) activeRun.value = null
      const message = list.find((item) => item.id === streamMessageId)
      if (message && message.status === 'pending') message.status = 'completed'
    }
  }

  async function cancel() {
    const run = activeRun.value
    try {
      if (run) await cancelAssistantRun(run.id)
    } finally {
      controller?.abort()
      controller = null
      loading.value = false
      activeRun.value = null
    }
  }

  async function respondPermission(response: 'once' | 'reject') {
    const run = activeRun.value
    if (!run?.permissionId) return
    await respondAssistantPermission(run.id, run.permissionId, response)
    const list = messagesByConversation.value[currentConversationId.value || 0]
    const message = list?.find((item) => item.permission?.permission_id === run.permissionId)
    if (message) message.permission = undefined
    activeRun.value = { ...run, permissionId: undefined }
  }

  async function handoff(messageId?: number) {
    const id = conversationIds.value.fast
    if (!id) return
    const target = await handoffAssistantConversation(id, messageId)
    mergeConversation(target)
    conversationIds.value.expert = target.id
    currentMode.value = 'expert'
    const history = await getAssistantMessages(target.id)
    messagesByConversation.value[target.id] = history.messages.map(mapMessage)
  }

  function clearCurrentConversation() {
    const id = currentConversationId.value
    if (id) messagesByConversation.value[id] = []
  }

  return {
    currentMode,
    conversations,
    currentConversations,
    currentConversationId,
    messages,
    activeRun,
    loading,
    expertAvailable,
    bindWorkspace,
    selectMode,
    selectConversation,
    newConversation,
    send,
    cancel,
    respondPermission,
    handoff,
    clearCurrentConversation,
  }
})
