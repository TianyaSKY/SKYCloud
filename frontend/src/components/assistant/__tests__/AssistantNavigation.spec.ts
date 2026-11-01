import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, disposePinia, getActivePinia, setActivePinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { defineComponent } from 'vue'
import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'

import App from '@/App.vue'
import request from '@/api/request'
import { useAssistantStore } from '@/stores/assistant'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import type {
  AssistantConversation,
  AssistantEvent,
  AssistantMessageRecord,
  AssistantMode,
  AssistantRunRecord,
} from '@/api/assistant'

const notifications = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn() }))
vi.mock('@arco-design/web-vue', () => ({
  Message: notifications,
  Modal: { confirm: vi.fn() },
  Button: { template: '<button><slot /></button>' },
}))
// 应用路由由本测试自己创建并传给 App；这里只隔离 request.ts 的 401 跳转依赖。
vi.mock('@/router', () => ({
  default: { currentRoute: { value: { name: 'home' } }, push: vi.fn() },
}))

/** Minimal real-HTTP stand-in for the assistant endpoints the widget calls through Axios. */
interface Server {
  lists: Array<{ workspace: number; mode: string }>
  creates: string[]
  conversations: Record<number, AssistantConversation[]>
  history: Record<number, AssistantMessageRecord[]>
  active: Record<number, AssistantRunRecord | null>
  listError: string | null
  cancelError: string | null
}

const server: Server = {
  lists: [],
  creates: [],
  conversations: {},
  history: {},
  active: {},
  listError: null,
  cancelError: null,
}

function conversation(id: number, workspaceId: number, mode: AssistantMode = 'fast'): AssistantConversation {
  return {
    id, workspace_id: workspaceId, user_id: 7, mode, title: 'conversation', status: 'active',
    source_conversation_id: null, opencode_runtime_id: null, opencode_session_id: null,
    created_at: null, updated_at: null, last_message_at: null,
  }
}

function record(id: number, content: string): AssistantMessageRecord {
  return {
    id, conversation_id: 11, role: 'assistant', content, status: 'completed',
    provider_message_id: null, metadata: {}, usage: {}, sequence: 1, created_at: null, updated_at: null,
  }
}

function runRecord(id: number, status: AssistantRunRecord['status']): AssistantRunRecord {
  return {
    id, conversation_id: 11, user_message_id: null, assistant_message_id: null, engine: 'fast', status,
    request_id: null, opencode_session_id: null, pending_permission: null, pending_permission_id: null,
    cancel_requested: false, error_code: null, error_message: null, metadata: {},
    started_at: null, completed_at: null, created_at: null,
  }
}

function findConversation(id: number): AssistantConversation {
  for (const list of Object.values(server.conversations)) {
    const found = list.find(item => item.id === id)
    if (found) return found
  }
  throw new Error(`unknown conversation ${id}`)
}

/** Public error envelope so the request interceptor surfaces the server message. */
function httpError(config: InternalAxiosRequestConfig, message: string) {
  return new AxiosError(message, 'ERR_BAD_RESPONSE', config, undefined, {
    status: 500,
    statusText: 'Internal Server Error',
    headers: {},
    config,
    data: { success: false, error: { code: 'ASSISTANT_UNAVAILABLE', message } },
  })
}

const adapter: AxiosAdapter = async (config) => {
  const url = config.url ?? ''
  const method = (config.method ?? 'get').toLowerCase()
  const workspaceId = Number(config.headers?.['X-Workspace-Id'] ?? 0)
  const reply = (data: unknown) => ({
    data: { success: true, data }, status: 200, statusText: 'OK', headers: {}, config,
  })
  if (url === '/assistant/conversations' && method === 'get') {
    server.lists.push({ workspace: workspaceId, mode: String(config.params?.mode ?? '') })
    if (server.listError) throw httpError(config, server.listError)
    return reply({ conversations: server.conversations[workspaceId] ?? [] })
  }
  if (url === '/assistant/conversations' && method === 'post') {
    const body = JSON.parse(String(config.data ?? '{}')) as { mode: AssistantMode }
    const created = conversation(100 + server.creates.length, workspaceId, body.mode)
    const list = (server.conversations[workspaceId] ??= [])
    list.push(created)
    server.creates.push(body.mode)
    server.history[created.id] = []
    return reply(created)
  }
  const history = /^\/assistant\/conversations\/(\d+)\/messages$/.exec(url)
  if (history && method === 'get') {
    const id = Number(history[1])
    return reply({ conversation: findConversation(id), messages: server.history[id] ?? [] })
  }
  const activeRun = /^\/assistant\/conversations\/(\d+)\/active-run$/.exec(url)
  if (activeRun && method === 'get') return reply(server.active[Number(activeRun[1])] ?? null)
  const cancel = /^\/assistant\/runs\/(\d+)\/cancel$/.exec(url)
  if (cancel && method === 'post') {
    if (server.cancelError) throw httpError(config, server.cancelError)
    return reply(runRecord(Number(cancel[1]), 'cancelled'))
  }
  throw new Error(`unexpected request ${method.toUpperCase()} ${url}`)
}

const fetchMock = vi.fn()

interface OpenStream {
  emit: (event: AssistantEvent) => void
  close: () => void
  signal: () => AbortSignal | null | undefined
}

/** Real SSE boundary: the store talks to this stream exactly like it talks to the API. */
function openStream(): OpenStream {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value } })
  fetchMock.mockResolvedValueOnce(new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }))
  const encoder = new TextEncoder()
  return {
    emit(event) { controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`)) },
    close() { controller.close() },
    signal: () => (fetchMock.mock.calls.at(-1)?.[1] as RequestInit | undefined)?.signal,
  }
}

const TextareaStub = defineComponent({
  props: { modelValue: { type: String, default: '' }, disabled: { type: Boolean, default: false } },
  emits: ['update:modelValue'],
  template: '<textarea :value="modelValue" :disabled="disabled" @input="$emit(\'update:modelValue\', $event.target.value)" />',
})

const RouteStub = defineComponent({ template: '<div class="route-stub" />' })

let pinia: Pinia
let wrapper: VueWrapper | undefined

async function mountApp(path = '/home'): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'login', component: RouteStub },
      { path: '/home', name: 'home', component: RouteStub },
      { path: '/inbox', name: 'inbox', component: RouteStub },
    ],
  })
  await router.push(path)
  await router.isReady()
  const mounted = mount(App, {
    global: {
      plugins: [pinia, router],
      stubs: {
        'a-textarea': TextareaStub,
        AssistantHeader: true,
        AssistantConversationList: true,
      },
    },
  })
  wrapper = mounted
  await flushPromises()
  return { wrapper: mounted, router }
}

beforeEach(() => {
  vi.resetAllMocks()
  server.lists = []
  server.creates = []
  server.conversations = { 11: [conversation(11, 11)] }
  server.history = { 11: [] }
  server.active = {}
  server.listError = null
  server.cancelError = null
  localStorage.clear()
  pinia = createPinia()
  setActivePinia(pinia)
  request.defaults.adapter = adapter
  vi.stubGlobal('fetch', fetchMock)
  useAuthStore().setAuth('session-token', { id: 7, username: 'user', role: 'user' })
  useWorkspaceStore().currentWorkspace = { id: 11, name: 'space-11', my_role: 'editor' } as never
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.unstubAllGlobals()
  const active = getActivePinia()
  if (active) disposePinia(active)
})

describe('assistant application-level lifecycle', () => {
  it('keeps the conversation, draft and running stream alive across navigation', async () => {
    const mounted = await mountApp()
    const assistant = useAssistantStore()
    await vi.waitFor(() => expect(assistant.initialized).toBe(true))

    await wrapper!.find('.assistant-trigger').trigger('click')
    await wrapper!.find('textarea').setValue('first question')
    expect(assistant.draft).toBe('first question')

    const stream = openStream()
    await wrapper!.find('form').trigger('submit')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    stream.emit({ type: 'run_started', run_id: 501, message_id: 601, payload: {} })
    await vi.waitFor(() => expect(assistant.draft).toBe(''))

    await wrapper!.find('textarea').setValue('typed while running')
    const conversationId = assistant.currentConversationId
    await mounted.router.push('/inbox')
    await flushPromises()

    expect(assistant.draft).toBe('typed while running')
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('typed while running')
    expect(assistant.initialized).toBe(true)
    expect(assistant.currentConversationId).toBe(conversationId)
    expect(stream.signal()?.aborted).toBe(false)
    expect((wrapper!.find('.assistant-widget').element as HTMLElement).style.display).toBe('none')

    stream.emit({ type: 'token', run_id: 501, payload: { content: 'finished in background' } })
    stream.emit({ type: 'done', run_id: 501, payload: {} })
    stream.close()
    await vi.waitFor(() => expect(assistant.messages.at(-1)?.status).toBe('completed'))

    await mounted.router.push('/home')
    await flushPromises()
    expect(wrapper!.text()).toContain('finished in background')
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('typed while running')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not create an assistant conversation when the app opens outside the home route', async () => {
    const mounted = await mountApp('/inbox')
    const assistant = useAssistantStore()
    await flushPromises()

    expect(assistant.context).toEqual({ userId: 7, sessionVersion: 1, workspaceId: 11 })
    expect(assistant.initialized).toBe(false)
    expect(server.lists).toHaveLength(0)
    expect(server.creates).toHaveLength(0)
    expect((wrapper!.find('.assistant-widget').element as HTMLElement).style.display).toBe('none')

    await mounted.router.push('/home')
    await vi.waitFor(() => expect(assistant.initialized).toBe(true))
    expect(server.lists.map(call => call.workspace)).toEqual([11])
    expect(server.creates).toHaveLength(0)
  })

  it('keeps the draft when the run is rejected before acceptance', async () => {
    await mountApp()
    const assistant = useAssistantStore()
    await vi.waitFor(() => expect(assistant.initialized).toBe(true))

    await wrapper!.find('.assistant-trigger').trigger('click')
    await wrapper!.find('textarea').setValue('retry me after the rejection')
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      success: false, error: { code: 'ASSISTANT_BUSY', message: '助手忙碌，请稍后再试' },
    }), { status: 503, headers: { 'Content-Type': 'application/json' } }))
    await wrapper!.find('form').trigger('submit')
    await vi.waitFor(() => expect(notifications.error).toHaveBeenCalledWith('助手忙碌，请稍后再试'))

    expect(assistant.activeRun).toBeNull()
    expect(assistant.draft).toBe('retry me after the rejection')
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('retry me after the rejection')
  })

  it.each(['done', 'error'] as const)('does not clear a newer draft when the run ends with %s', async terminal => {
    await mountApp()
    const assistant = useAssistantStore()
    await vi.waitFor(() => expect(assistant.initialized).toBe(true))

    await wrapper!.find('.assistant-trigger').trigger('click')
    await wrapper!.find('textarea').setValue('first question')
    const stream = openStream()
    await wrapper!.find('form').trigger('submit')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    stream.emit({ type: 'run_started', run_id: 501, message_id: 601, payload: {} })
    await vi.waitFor(() => expect(assistant.draft).toBe(''))

    await wrapper!.find('textarea').setValue('next question typed during the run')
    stream.emit({ type: 'token', run_id: 501, payload: { content: 'partial answer' } })
    stream.emit({ type: terminal, run_id: 501, payload: {} })
    stream.close()
    await vi.waitFor(() => expect(assistant.loading).toBe(false))

    expect(assistant.draft).toBe('next question typed during the run')
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).value).toBe('next question typed during the run')
  })

  it('retries initialization when the user reopens the widget after a failed load', async () => {
    server.listError = '助手初始化失败'
    await mountApp()
    const assistant = useAssistantStore()
    await vi.waitFor(() => {
      expect(notifications.error).toHaveBeenCalledWith('助手初始化失败')
      expect(assistant.initializing).toBe(false)
    })
    expect(assistant.initialized).toBe(false)
    expect(server.lists).toHaveLength(1)

    await wrapper!.find('.assistant-trigger').trigger('click')
    await vi.waitFor(() => expect(server.lists).toHaveLength(2))
    await vi.waitFor(() => expect(assistant.initializing).toBe(false))
    expect(assistant.initialized).toBe(false)
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).disabled).toBe(true)

    await wrapper!.find('.assistant-trigger').trigger('click')
    server.listError = null
    await wrapper!.find('.assistant-trigger').trigger('click')
    await vi.waitFor(() => expect(assistant.initialized).toBe(true))
    expect(server.lists).toHaveLength(3)
    expect((wrapper!.find('textarea').element as HTMLTextAreaElement).disabled).toBe(false)

    await wrapper!.find('textarea').setValue('after the retry')
    const stream = openStream()
    await wrapper!.find('form').trigger('submit')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    stream.emit({ type: 'run_started', run_id: 501, message_id: 601, payload: {} })
    stream.emit({ type: 'token', run_id: 501, payload: { content: 'recovered answer' } })
    stream.emit({ type: 'done', run_id: 501, payload: {} })
    stream.close()
    await vi.waitFor(() => expect(assistant.messages.at(-1)?.content).toBe('recovered answer'))
    expect(wrapper!.text()).toContain('recovered answer')
  })

  it('drops the old workspace stream and draft and initializes the new context', async () => {
    server.history[11] = [record(201, 'old workspace answer')]
    await mountApp()
    const assistant = useAssistantStore()
    await vi.waitFor(() => expect(assistant.initialized).toBe(true))
    await wrapper!.find('.assistant-trigger').trigger('click')
    expect(wrapper!.text()).toContain('old workspace answer')

    await wrapper!.find('textarea').setValue('draft from the old workspace')
    const stream = openStream()
    await wrapper!.find('form').trigger('submit')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    stream.emit({ type: 'run_started', run_id: 501, message_id: 601, payload: {} })
    await vi.waitFor(() => expect(assistant.draft).toBe(''))

    server.conversations[12] = [conversation(21, 12)]
    server.history[21] = [record(301, 'new workspace answer')]
    useWorkspaceStore().switchWorkspace({ id: 12, name: 'space-12', my_role: 'editor' } as never)
    await vi.waitFor(() => {
      expect(assistant.currentConversationId).toBe(21)
      expect(assistant.initialized).toBe(true)
    })

    expect(stream.signal()?.aborted).toBe(true)
    expect(assistant.draft).toBe('')
    expect(assistant.messages.map(item => item.content)).toEqual(['new workspace answer'])
    expect(wrapper!.text()).toContain('new workspace answer')
    expect(wrapper!.text()).not.toContain('old workspace answer')

    await wrapper!.find('textarea').setValue('question for the new workspace')
    const next = openStream()
    await wrapper!.find('form').trigger('submit')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    next.emit({ type: 'run_started', run_id: 502, message_id: 602, payload: {} })
    next.emit({ type: 'token', run_id: 502, payload: { content: 'new answer' } })
    next.emit({ type: 'done', run_id: 502, payload: {} })
    next.close()
    await vi.waitFor(() => expect(assistant.messages.at(-1)?.content).toBe('new answer'))
    expect(wrapper!.text()).toContain('new answer')
  })

  it('treats a replaced login session in the same workspace as a new context', async () => {
    server.history[11] = [record(202, 'session one answer')]
    await mountApp()
    const assistant = useAssistantStore()
    await vi.waitFor(() => expect(assistant.initialized).toBe(true))
    await wrapper!.find('.assistant-trigger').trigger('click')
    await wrapper!.find('textarea').setValue('draft from session one')

    server.history[11] = [record(203, 'session two answer')]
    useAuthStore().setAuth('rotated-session-token', { id: 7, username: 'user', role: 'user' })
    useWorkspaceStore().currentWorkspace = { id: 11, name: 'space-11', my_role: 'editor' } as never
    await vi.waitFor(() => expect(assistant.context).toEqual({ userId: 7, sessionVersion: 2, workspaceId: 11 }))
    await vi.waitFor(() => {
      expect(assistant.messages.map(item => item.content)).toEqual(['session two answer'])
      expect(assistant.initialized).toBe(true)
    })

    expect(assistant.draft).toBe('')
    expect(wrapper!.text()).toContain('session two answer')
    expect(wrapper!.text()).not.toContain('session one answer')

    await wrapper!.find('textarea').setValue('question after the login was replaced')
    const stream = openStream()
    await wrapper!.find('form').trigger('submit')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    stream.emit({ type: 'run_started', run_id: 503, message_id: 603, payload: {} })
    stream.emit({ type: 'token', run_id: 503, payload: { content: 'session two reply' } })
    stream.emit({ type: 'done', run_id: 503, payload: {} })
    stream.close()
    await vi.waitFor(() => expect(assistant.messages.at(-1)?.content).toBe('session two reply'))
    expect(assistant.draft).toBe('')
  })

  it('reports a failed cancellation and keeps the running task', async () => {
    await mountApp()
    const assistant = useAssistantStore()
    await vi.waitFor(() => expect(assistant.initialized).toBe(true))
    await wrapper!.find('.assistant-trigger').trigger('click')
    await wrapper!.find('textarea').setValue('long running task')
    const stream = openStream()
    await wrapper!.find('form').trigger('submit')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    stream.emit({ type: 'run_started', run_id: 501, message_id: 601, payload: {} })
    await vi.waitFor(() => expect(assistant.activeRun?.id).toBe(501))

    server.cancelError = '取消失败，请重试'
    await wrapper!.find('button[aria-label="停止任务"]').trigger('click')
    await vi.waitFor(() => expect(notifications.error).toHaveBeenCalledWith('取消失败，请重试'))
    expect(assistant.activeRun?.id).toBe(501)

    stream.emit({ type: 'done', run_id: 501, payload: {} })
    stream.close()
    await vi.waitFor(() => expect(assistant.activeRun).toBeNull())
  })
})
