import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { Message } from '@arco-design/web-vue'

import {
  cancelAssistantRun,
  createAssistantConversation,
  deleteAssistantConversation,
  getActiveAssistantRun,
  getActiveExpertRun,
  getAssistantMessages,
  handoffAssistantConversation,
  listAssistantConversations,
  respondAssistantPermission,
  updateAssistantConversation,
  type AssistantConversation,
  type AssistantEvent,
  type AssistantMessageRecord,
  type AssistantMode,
  type AssistantRunRecord,
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
  tool?: unknown
}

export interface AssistantMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  title?: string
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
  const activeRun = ref<{ id: number; messageId: number; permissionId?: string; cancelRequested?: boolean } | null>(null)
  const loading = ref(false)
  const initialized = ref(false)
  const initializing = ref(false)
  const expertAvailable = ref(true)

  let controller: AbortController | null = null
  let localMessageSequence = -1
  let stateVersion = 0
  const isCurrent = (version: number) => version === stateVersion

  const currentConversationId = computed(() => conversationIds.value[currentMode.value])
  const currentConversation = computed(() =>
    conversations.value.find((conversation) => conversation.id === currentConversationId.value) || null,
  )
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
      title: typeof metadata.title === 'string' ? metadata.title : undefined,
      status: record.status,
      keywords: typeof metadata.keywords === 'string' ? metadata.keywords : undefined,
      sources: Array.isArray(metadata.sources) ? (metadata.sources as Array<Record<string, unknown>>) : undefined,
      tools: Array.isArray(metadata.tool_calls) ? (metadata.tool_calls as AssistantToolCall[]) : undefined,
      diff: Array.isArray(metadata.diff) ? (metadata.diff as Array<Record<string, unknown>>) : undefined,
      usage: record.usage,
    }
  }

  function legacyTitle(line: string): string | undefined {
    const value = line.trim()
    let title = ''
    if (value.startsWith('#')) title = value.replace(/^#+\s*/, '')
    else if (value.startsWith('**') && value.endsWith('**')) title = value.slice(2, -2).trim()
    else if (value.startsWith('__') && value.endsWith('__')) title = value.slice(2, -2).trim()
    return title && title.length <= 160 ? title : undefined
  }

  function normalizeLegacyExpertMessage(message: AssistantMessage, previousUserContent: string) {
    if (message.role !== 'assistant' || message.title || !previousUserContent) return
    const content = message.content.trimStart()
    if (!content.startsWith(previousUserContent)) return

    let remainder = content.slice(previousUserContent.length).trimStart()
    const titles: string[] = []
    while (remainder) {
      const newline = remainder.indexOf('\n')
      const line = newline === -1 ? remainder : remainder.slice(0, newline)
      const title = legacyTitle(line)
      if (!title) break
      titles.push(title)
      remainder = newline === -1 ? '' : remainder.slice(newline + 1).trimStart()
    }
    if (!titles.length) return
    message.title = titles[0]
    message.content = remainder.trimStart()
  }

  function mapMessages(records: AssistantMessageRecord[], mode: AssistantMode) {
    let previousUserContent = ''
    return records.map((record) => {
      const message = mapMessage(record)
      if (message.role === 'user') previousUserContent = message.content
      else if (mode === 'expert') normalizeLegacyExpertMessage(message, previousUserContent)
      return message
    })
  }

  function mergeConversation(conversation: AssistantConversation) {
    const index = conversations.value.findIndex((item) => item.id === conversation.id)
    if (index === -1) conversations.value.push(conversation)
    else conversations.value[index] = conversation
  }

  function applyActiveRun(run: AssistantRunRecord | null) {
    if (!run || ['completed', 'failed', 'cancelled'].includes(run.status)) {
      activeRun.value = null
      return
    }
    activeRun.value = {
      id: run.id,
      messageId: run.assistant_message_id || 0,
      permissionId: run.pending_permission_id || undefined,
      cancelRequested: run.cancel_requested,
    }
  }

  async function refreshActiveRun(mode: AssistantMode, conversationId: number, version = stateVersion) {
    try {
      const run = mode === 'expert'
        ? await getActiveExpertRun()
        : await getActiveAssistantRun(conversationId)
      if (isCurrent(version) && currentMode.value === mode && currentConversationId.value === conversationId) applyActiveRun(run)
    } catch (error) {
      if (isCurrent(version) && currentMode.value === mode && currentConversationId.value === conversationId) initialized.value = false
      throw error
    }
  }

  async function loadMode(mode: AssistantMode, createIfMissing = true, version = stateVersion) {
    if (!workspaceId.value) return
    try {
      const result = await listAssistantConversations(mode)
      if (!isCurrent(version)) return
      result.conversations.forEach(mergeConversation)
      let conversation = result.conversations.find((item) => item.status === 'active')
      if (!conversation && createIfMissing) {
        conversation = await createAssistantConversation(mode, mode === 'fast' ? '快速问答' : '专家任务')
        if (!isCurrent(version)) return
        mergeConversation(conversation)
      }
      if (conversation) {
        conversationIds.value[mode] = conversation.id
        const history = await getAssistantMessages(conversation.id)
        if (!isCurrent(version)) return
        messagesByConversation.value[conversation.id] = mapMessages(history.messages, mode)
        await refreshActiveRun(mode, conversation.id, version)
      } else if (mode === currentMode.value) {
        conversationIds.value[mode] = null
        activeRun.value = null
      }
      if (isCurrent(version) && mode === 'expert') expertAvailable.value = true
    } catch (error) {
      if (!isCurrent(version)) return
      if (mode === 'expert' && error instanceof BizError && error.status === 403) {
        expertAvailable.value = false
        return
      }
      throw error
    }
  }

  async function bindWorkspace(id: number | null, force = false) {
    if (!force && workspaceId.value === id && (initialized.value || initializing.value)) return
    const version = ++stateVersion
    initializing.value = true
    try {
      controller?.abort()
      controller = null
      loading.value = false
      expertAvailable.value = true
      workspaceId.value = id
      currentMode.value = 'fast'
      conversations.value = []
      conversationIds.value = { fast: null, expert: null }
      messagesByConversation.value = {}
      activeRun.value = null
      initialized.value = false
      if (!id) return
      await loadMode('fast', true, version)
      if (isCurrent(version)) initialized.value = true
    } catch (error) {
      if (isCurrent(version)) throw error
    } finally {
      if (isCurrent(version)) initializing.value = false
    }
  }

  async function selectMode(mode: AssistantMode) {
    if (!initialized.value) return
    const workspace = useWorkspaceStore()
    if (loading.value) return
    if (mode === 'expert' && workspace.canWrite) expertAvailable.value = true
    if (mode === 'expert' && (!expertAvailable.value || !workspace.canWrite)) return
    const version = ++stateVersion
    initializing.value = true
    try {
      activeRun.value = null
      currentMode.value = mode
      if (!conversationIds.value[mode]) await loadMode(mode, true, version)
      if (!isCurrent(version)) return
      const conversationId = conversationIds.value[mode]
      if (conversationId) await refreshActiveRun(mode, conversationId, version)
    } catch (error) {
      if (isCurrent(version)) throw error
    } finally {
      if (isCurrent(version)) initializing.value = false
    }
  }

  async function selectConversation(id: number) {
    if (!initialized.value) return
    if (loading.value) return
    const conversation = conversations.value.find((item) => item.id === id)
    if (!conversation) return
    if (conversation.mode === 'expert' && !useWorkspaceStore().canWrite) return
    const version = ++stateVersion
    initializing.value = true
    try {
      activeRun.value = null
      currentMode.value = conversation.mode
      conversationIds.value[conversation.mode] = id
      if (!messagesByConversation.value[id]) {
        const history = await getAssistantMessages(id)
        if (!isCurrent(version)) return
        messagesByConversation.value[id] = mapMessages(history.messages, conversation.mode)
      }
      await refreshActiveRun(conversation.mode, id, version)
    } catch (error) {
      if (isCurrent(version)) throw error
    } finally {
      if (isCurrent(version)) initializing.value = false
    }
  }

  async function newConversation() {
    if (!initialized.value) return
    if (!workspaceId.value) return
    if (loading.value || initializing.value) return
    const workspace = useWorkspaceStore()
    if (currentMode.value === 'expert' && (!expertAvailable.value || !workspace.canWrite)) return
    const version = ++stateVersion
    initializing.value = true
    try {
      activeRun.value = null
      const conversation = await createAssistantConversation(
        currentMode.value,
        currentMode.value === 'fast' ? '快速问答' : '专家任务',
      )
      if (!isCurrent(version)) return
      mergeConversation(conversation)
      conversationIds.value[currentMode.value] = conversation.id
      messagesByConversation.value[conversation.id] = []
      await refreshActiveRun(currentMode.value, conversation.id, version)
    } catch (error) {
      if (isCurrent(version)) throw error
    } finally {
      if (isCurrent(version)) initializing.value = false
    }
  }

  async function renameConversation(id: number, title: string) {
    const version = stateVersion
    try {
      const conversation = await updateAssistantConversation(id, { title })
      if (isCurrent(version)) mergeConversation(conversation)
    } catch (error) {
      if (isCurrent(version)) throw error
    }
  }

  async function archiveConversation(id: number) {
    if (!initialized.value) return
    if (loading.value || initializing.value || activeRun.value) return
    const version = ++stateVersion
    initializing.value = true
    try {
      const conversation = await updateAssistantConversation(id, { status: 'archived' })
      if (!isCurrent(version)) return
      mergeConversation(conversation)
      if (conversationIds.value[conversation.mode] === id) {
        conversationIds.value[conversation.mode] = null
        messagesByConversation.value[id] = []
        await loadMode(conversation.mode)
      }
    } catch (error) {
      if (isCurrent(version)) throw error
    } finally {
      if (isCurrent(version)) initializing.value = false
    }
  }

  async function restoreConversation(id: number) {
    if (!initialized.value) return
    if (loading.value) return
    const version = ++stateVersion
    initializing.value = true
    try {
      activeRun.value = null
      const conversation = await updateAssistantConversation(id, { status: 'active' })
      if (!isCurrent(version)) return
      if (conversation.mode === 'expert' && !useWorkspaceStore().canWrite) return
      mergeConversation(conversation)
      conversationIds.value[conversation.mode] = id
      const history = await getAssistantMessages(id)
      if (!isCurrent(version)) return
      messagesByConversation.value[id] = mapMessages(history.messages, conversation.mode)
      currentMode.value = conversation.mode
      await refreshActiveRun(conversation.mode, id, version)
    } catch (error) {
      if (isCurrent(version)) throw error
    } finally {
      if (isCurrent(version)) initializing.value = false
    }
  }

  async function removeConversation(id: number) {
    if (!initialized.value) return
    if (loading.value || initializing.value || activeRun.value) return
    const version = ++stateVersion
    initializing.value = true
    try {
      const conversation = conversations.value.find((item) => item.id === id)
      await deleteAssistantConversation(id)
      if (!isCurrent(version)) return
      conversations.value = conversations.value.filter((item) => item.id !== id)
      delete messagesByConversation.value[id]
      if (conversation && conversationIds.value[conversation.mode] === id) {
        conversationIds.value[conversation.mode] = null
        await loadMode(conversation.mode)
      }
    } catch (error) {
      if (isCurrent(version)) throw error
    } finally {
      if (isCurrent(version)) initializing.value = false
    }
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
    } else if (event.type === 'title') {
      message.title = String(payload.content || '') || undefined
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
        tool: payload.tool,
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
      message.permission = undefined
      if (activeRun.value?.id === event.run_id) activeRun.value = null
    } else if (event.type === 'cancelled') {
      message.status = 'cancelled'
      message.permission = undefined
      if (activeRun.value?.id === event.run_id) activeRun.value = null
    } else if (event.type === 'error') {
      message.status = 'failed'
      message.permission = undefined
      if (activeRun.value?.id === event.run_id) activeRun.value = null
    }
  }

  async function send(query: string) {
    if (!query.trim() || loading.value || initializing.value || !initialized.value || activeRun.value || !workspaceId.value) return
    if (currentMode.value === 'expert' && (!expertAvailable.value || !useWorkspaceStore().canWrite)) return
    if (currentConversation.value?.status === 'archived') return
    const version = stateVersion
    const mode = currentMode.value
    const streamController = new AbortController()
    controller?.abort()
    controller = streamController
    loading.value = true
    let conversationId: number | null = null
    let streamMessageId = 0
    let terminalReceived = false
    try {
      if (!conversationIds.value[mode]) await loadMode(mode, true, version)
      if (!isCurrent(version) || streamController.signal.aborted) return
      conversationId = conversationIds.value[mode]
      if (!conversationId || activeRun.value) return
      const list = messagesByConversation.value[conversationId] || (messagesByConversation.value[conversationId] = [])
      const assistantMessage: AssistantMessage = {
        id: localMessageSequence--, role: 'assistant', content: '', status: 'pending', local: true,
      }
      list.push({ id: localMessageSequence--, role: 'user', content: query.trim(), status: 'completed', local: true }, assistantMessage)
      streamMessageId = assistantMessage.id
      await sseRequest(
        `/assistant/conversations/${conversationId}/messages/stream`,
        { query: query.trim() },
        {
          signal: streamController.signal,
          onEvent: (event) => {
            if (!isCurrent(version) || streamController.signal.aborted || terminalReceived) return
            applyEvent(event, streamMessageId)
            if (event.type === 'run_started' && event.message_id) streamMessageId = event.message_id
            if (['done', 'error', 'cancelled'].includes(event.type)) terminalReceived = true
          },
        },
      )
      if (!terminalReceived && !streamController.signal.aborted) throw new Error('助手连接已中断，任务状态正在重新确认。')
    } catch (error) {
      if (!isCurrent(version) || streamController.signal.aborted) return
      const message = messagesByConversation.value[conversationId || 0]?.find((item) => item.id === streamMessageId)
      if (message) {
        message.status = 'failed'
        message.content = message.content || '抱歉，任务执行失败。'
      }
      Message.error((error as Error).message || '发送失败')
    } finally {
      if (isCurrent(version) && controller === streamController) {
        if (!terminalReceived && conversationId) {
          // A disconnected browser stream does not prove that the server run stopped.
          try { await refreshActiveRun(mode, conversationId, version) } catch { /* Retain the known run for cancellation/retry. */ }
        }
        if (isCurrent(version) && controller === streamController) {
          loading.value = false
          controller = null
        }
      }
    }
  }

  async function cancel() {
    const version = stateVersion
    const run = activeRun.value
    const streamController = controller
    let terminalStatus: AssistantMessage['status'] = 'cancelled'
    if (run) {
      const result = await cancelAssistantRun(run.id)
      if (!isCurrent(version) || activeRun.value?.id !== run.id) return
      if (!['completed', 'failed', 'cancelled'].includes(result.status)) {
        activeRun.value = { ...activeRun.value, cancelRequested: true }
        // The API acknowledges cancel_requested before the engine actually stops.
        if (!streamController && currentConversationId.value) {
          await refreshActiveRun(currentMode.value, currentConversationId.value, version)
        }
        return
      }
      terminalStatus = result.status as AssistantMessage['status']
    }
    if (!isCurrent(version) || controller !== streamController || activeRun.value?.id !== run?.id) return
    const nextVersion = ++stateVersion
    streamController?.abort()
    const message = run
      ? messages.value.find((item) => item.id === run.messageId)
      : [...messages.value].reverse().find((item) => ['pending', 'streaming'].includes(item.status))
    if (message) {
      message.status = terminalStatus
      message.permission = undefined
    }
    controller = null
    loading.value = false
    activeRun.value = null
    if (!run && currentConversationId.value) {
      try { await refreshActiveRun(currentMode.value, currentConversationId.value, nextVersion) } catch { /* A later selection can retry. */ }
    }
  }

  async function respondPermission(response: 'once' | 'reject') {
    const version = stateVersion
    const run = activeRun.value
    if (!run?.permissionId || run.cancelRequested || !useWorkspaceStore().canWrite) return
    const permissionId = run.permissionId
    await respondAssistantPermission(run.id, permissionId, response)
    if (!isCurrent(version) || activeRun.value?.id !== run.id || activeRun.value.permissionId !== permissionId) return
    const message = messages.value.find((item) => item.permission?.permission_id === permissionId)
    if (message) message.permission = undefined
    activeRun.value = { ...activeRun.value, permissionId: undefined }
  }

  async function handoff(messageId?: number) {
    if (!initialized.value) return
    const id = conversationIds.value.fast
    if (!id || loading.value || initializing.value || !useWorkspaceStore().canWrite) return
    const version = ++stateVersion
    initializing.value = true
    try {
      activeRun.value = null
      const target = await handoffAssistantConversation(id, messageId)
      if (!isCurrent(version)) return
      mergeConversation(target)
      conversationIds.value.expert = target.id
      currentMode.value = 'expert'
      const history = await getAssistantMessages(target.id)
      if (!isCurrent(version)) return
      messagesByConversation.value[target.id] = mapMessages(history.messages, 'expert')
      await refreshActiveRun('expert', target.id, version)
    } catch (error) {
      if (isCurrent(version)) throw error
    } finally {
      if (isCurrent(version)) initializing.value = false
    }
  }

  function clearCurrentConversation() {
    if (loading.value || activeRun.value) return
    const id = currentConversationId.value
    if (id) messagesByConversation.value[id] = []
  }

  return {
    currentMode,
    conversations,
    currentConversations,
    currentConversationId,
    currentConversation,
    messages,
    activeRun,
    loading,
    initialized,
    initializing,
    expertAvailable,
    bindWorkspace,
    selectMode,
    selectConversation,
    newConversation,
    renameConversation,
    archiveConversation,
    restoreConversation,
    removeConversation,
    send,
    cancel,
    respondPermission,
    handoff,
    clearCurrentConversation,
  }
})
