import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { AssistantConversation, AssistantMessageRecord, AssistantMode, AssistantRunRecord, AssistantEvent } from '@/api/assistant'
import { useAssistantStore } from '../assistant'

const mocks = vi.hoisted(() => ({
  list: vi.fn(), create: vi.fn(), messages: vi.fn(), active: vi.fn(), expertActive: vi.fn(),
  update: vi.fn(), remove: vi.fn(), cancel: vi.fn(), permission: vi.fn(), handoff: vi.fn(),
  stream: vi.fn(), error: vi.fn(), workspace: { canWrite: true },
}))
vi.mock('@/api/assistant', () => ({
  listAssistantConversations: mocks.list, createAssistantConversation: mocks.create,
  getAssistantMessages: mocks.messages, getActiveAssistantRun: mocks.active,
  getActiveExpertRun: mocks.expertActive, updateAssistantConversation: mocks.update,
  deleteAssistantConversation: mocks.remove, cancelAssistantRun: mocks.cancel,
  respondAssistantPermission: mocks.permission, handoffAssistantConversation: mocks.handoff,
}))
vi.mock('@/api/request', () => ({ BizError: class extends Error { status = 403 } }))
vi.mock('@/utils/sseRequest', () => ({ sseRequest: mocks.stream }))
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => mocks.workspace }))
vi.mock('@arco-design/web-vue', () => ({ Message: { error: mocks.error } }))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function conversation(id = 11, mode: AssistantMode = 'fast', workspace = 1): AssistantConversation {
  return { id, mode, workspace_id: workspace, user_id: 7, title: 'conversation', status: 'active',
    source_conversation_id: null, opencode_runtime_id: null, opencode_session_id: null,
    created_at: null, updated_at: null, last_message_at: null }
}
function record(id: number, content = 'history'): AssistantMessageRecord {
  return { id, conversation_id: 11, role: 'assistant', content, status: 'completed',
    provider_message_id: null, metadata: {}, usage: {}, sequence: 1, created_at: null, updated_at: null }
}
function run(id = 501, permission: string | null = null): AssistantRunRecord {
  return { id, conversation_id: 11, user_message_id: 600, assistant_message_id: 601, engine: 'expert',
    status: permission ? 'waiting_permission' : 'running', request_id: null, opencode_session_id: null,
    pending_permission_id: permission, cancel_requested: false, error_code: null, error_message: null,
    metadata: {}, started_at: null, completed_at: null, created_at: null }
}
function runningStream() {
  const pending = deferred<void>()
  let emit!: (event: AssistantEvent) => void
  let signal!: AbortSignal
  mocks.stream.mockImplementation((_path, _body, options) => {
    emit = options.onEvent; signal = options.signal
    emit({ type: 'run_started', run_id: 501, message_id: 601, payload: {} })
    return pending.promise
  })
  return { pending, emit: (event: AssistantEvent) => emit(event), signal: () => signal }
}

beforeEach(() => {
  vi.resetAllMocks(); setActivePinia(createPinia()); mocks.workspace.canWrite = true
  mocks.list.mockResolvedValue({ conversations: [conversation()] })
  mocks.messages.mockResolvedValue({ conversation: conversation(), messages: [] })
  mocks.active.mockResolvedValue(null); mocks.expertActive.mockResolvedValue(null)
  mocks.create.mockResolvedValue(conversation(21))
  mocks.cancel.mockResolvedValue({ ...run(), status: 'cancelled' }); mocks.permission.mockResolvedValue(run())
  mocks.stream.mockImplementation(async (_path, _body, options) => {
    options.onEvent({ type: 'run_started', run_id: 501, message_id: 601, payload: {} })
    options.onEvent({ type: 'token', run_id: 501, payload: { content: 'answer' } })
    options.onEvent({ type: 'done', run_id: 501, payload: {} })
  })
})

describe('assistant workspace and conversation isolation', () => {
  it('does not adopt a late list or create a conversation in a former workspace', async () => {
    const old = deferred<{ conversations: AssistantConversation[] }>()
    mocks.list.mockReturnValueOnce(old.promise).mockResolvedValueOnce({ conversations: [conversation(22, 'fast', 2)] })
    const store = useAssistantStore(); const first = store.bindWorkspace(1)
    await store.bindWorkspace(2); old.resolve({ conversations: [] }); await first
    expect(store.currentConversationId).toBe(22)
    expect(store.conversations.map(c => c.id)).toEqual([22]); expect(mocks.create).not.toHaveBeenCalled()
    expect(store.initializing).toBe(false)
  })
  it('does not adopt old message history after a workspace switch', async () => {
    const old = deferred<{ conversation: AssistantConversation; messages: AssistantMessageRecord[] }>()
    mocks.messages.mockReturnValueOnce(old.promise).mockResolvedValueOnce({ conversation: conversation(22), messages: [record(9, 'new workspace')] })
    mocks.list.mockResolvedValueOnce({ conversations: [conversation()] }).mockResolvedValueOnce({ conversations: [conversation(22, 'fast', 2)] })
    const store = useAssistantStore(); const first = store.bindWorkspace(1)
    await vi.waitFor(() => expect(mocks.messages).toHaveBeenCalledTimes(1))
    await store.bindWorkspace(2); old.resolve({ conversation: conversation(), messages: [record(8, 'private old history')] }); await first
    expect(store.messages.map(m => m.content)).toEqual(['new workspace'])
  })
  it('keeps the latest selected conversation when earlier run lookups finish late', async () => {
    mocks.list.mockResolvedValue({ conversations: [conversation(), conversation(12), conversation(13)] })
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const old = deferred<AssistantRunRecord | null>(); mocks.active.mockReturnValueOnce(old.promise).mockResolvedValueOnce(null)
    const first = store.selectConversation(12)
    await vi.waitFor(() => expect(mocks.active).toHaveBeenCalledWith(12))
    await store.selectConversation(13); old.resolve(run()); await first
    expect(store.currentConversationId).toBe(13); expect(store.activeRun).toBeNull()
  })
  it('does not start a duplicate run when the selected conversation run lookup fails', async () => {
    mocks.list.mockResolvedValue({ conversations: [conversation(), conversation(12)] })
    const store = useAssistantStore(); await store.bindWorkspace(1)
    mocks.active.mockRejectedValueOnce(new Error('run lookup outage'))
    await expect(store.selectConversation(12)).rejects.toThrow('run lookup outage')
    expect(store.initialized).toBe(false)
    await store.send('unsafe duplicate'); expect(mocks.stream).not.toHaveBeenCalled()
    await store.bindWorkspace(1); await store.send('retry'); expect(mocks.stream).toHaveBeenCalledTimes(1)
  })
  it('blocks send while initialization is pending, rather than creating duplicate conversations', async () => {
    const pending = deferred<{ conversations: AssistantConversation[] }>(); mocks.list.mockReturnValue(pending.promise)
    const store = useAssistantStore(); const binding = store.bindWorkspace(1)
    await store.send('early'); expect(mocks.stream).not.toHaveBeenCalled(); expect(mocks.create).not.toHaveBeenCalled()
    pending.resolve({ conversations: [conversation()] }); await binding
    await store.send(' ready '); expect(mocks.stream).toHaveBeenCalledTimes(1)
    expect(store.messages.map(m => m.content)).toEqual(['ready', 'answer'])
  })
  it('coalesces an in-progress workspace initialization and retries a failed initialization', async () => {
    const pending = deferred<{ conversations: AssistantConversation[] }>()
    mocks.list.mockReturnValueOnce(pending.promise)
    const store = useAssistantStore(); const first = store.bindWorkspace(1)
    await store.bindWorkspace(1); expect(mocks.list).toHaveBeenCalledTimes(1)
    pending.reject(new Error('initial outage')); await expect(first).rejects.toThrow('initial outage')
    expect(store.initialized).toBe(false); expect(store.initializing).toBe(false)
    await store.bindWorkspace(1); expect(store.initialized).toBe(true)
    await store.send('retry'); expect(mocks.stream).toHaveBeenCalledTimes(1)
  })
  it('allows the new workspace to send and ignores old stream events/finally cleanup', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const old = runningStream(); const first = store.send('old'); old.emit({ type: 'token', payload: { content: 'old private answer' } })
    mocks.list.mockResolvedValue({ conversations: [conversation(22, 'fast', 2)] }); await store.bindWorkspace(2)
    expect(old.signal().aborted).toBe(true)
    const newer = runningStream(); const second = store.send('new')
    old.emit({ type: 'error', run_id: 501, payload: {} }); old.pending.resolve(); await first
    expect(store.loading).toBe(true); expect(store.activeRun?.id).toBe(501)
    expect(store.messages.map(m => m.content)).toEqual(['new', ''])
    newer.emit({ type: 'done', run_id: 501, payload: {} }); newer.pending.resolve(); await second
    expect(store.loading).toBe(false); expect(store.activeRun).toBeNull()
  })
  it('ignores a late expert forbidden response from an old workspace', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const pending = deferred<{ conversations: AssistantConversation[] }>(); mocks.list.mockReturnValueOnce(pending.promise)
    const selecting = store.selectMode('expert'); await store.bindWorkspace(2)
    pending.reject(new Error('former workspace denied')); await selecting
    expect(store.expertAvailable).toBe(true); expect(store.currentMode).toBe('fast')
  })
  it.each((['rename', 'restore', 'archive', 'remove', 'handoff'] as const).flatMap(action => [[action, false], [action, true]] as const))('ignores late %s responses from a former workspace (failed=%s)', async (action, failed) => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const pending = deferred<AssistantConversation>(); mocks.update.mockReturnValue(pending.promise); mocks.remove.mockReturnValue(pending.promise); mocks.handoff.mockReturnValue(pending.promise)
    const operation = action === 'rename' ? store.renameConversation(11, 'new') : action === 'restore' ? store.restoreConversation(11) : action === 'archive' ? store.archiveConversation(11) : action === 'remove' ? store.removeConversation(11) : store.handoff(5)
    mocks.list.mockResolvedValue({ conversations: [conversation(22, 'fast', 2)] }); await store.bindWorkspace(2)
    if (failed) pending.reject(new Error('former workspace failure'))
    else pending.resolve(conversation(11, 'expert'))
    await operation
    expect(store.conversations.map(c => c.id)).toEqual([22]); expect(store.currentMode).toBe('fast'); expect(store.currentConversationId).toBe(22)
  })
})

describe('assistant run and permission state', () => {
  it('keeps an interrupted server run available for cancellation and prevents a second send', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const stream = runningStream(); const sending = store.send('start')
    stream.emit({ type: 'token', payload: { content: 'partial' } }); mocks.active.mockResolvedValue(run())
    stream.pending.resolve(); await sending
    expect(store.messages.at(-1)?.status).toBe('failed'); expect(store.activeRun?.id).toBe(501)
    await store.send('duplicate'); expect(mocks.stream).toHaveBeenCalledTimes(1)
    await store.cancel(); expect(mocks.cancel).toHaveBeenCalledWith(501); expect(store.activeRun).toBeNull()
  })
  it('does not report an empty stream as a completed answer', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1); mocks.stream.mockResolvedValue(undefined)
    await store.send('start'); expect(store.messages.at(-1)?.status).toBe('failed'); expect(mocks.error).toHaveBeenCalled()
  })
  it('retains the known run if reconciliation fails after a transport error', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const stream = runningStream(); const sending = store.send('start'); mocks.active.mockRejectedValue(new Error('lookup outage'))
    stream.pending.reject(new Error('transport outage')); await sending
    expect(store.activeRun?.id).toBe(501); expect(store.loading).toBe(false)
  })
  it('does not clear a running task or disconnect it when server cancellation fails', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const stream = runningStream(); const sending = store.send('start'); mocks.cancel.mockRejectedValue(new Error('cancel outage'))
    await expect(store.cancel()).rejects.toThrow('cancel outage')
    expect(store.activeRun?.id).toBe(501); expect(store.loading).toBe(true); expect(stream.signal().aborted).toBe(false)
    stream.emit({ type: 'done', run_id: 501, payload: {} }); stream.pending.resolve(); await sending
  })
  it('keeps listening after a cancellation acknowledgement until the engine actually stops', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const stream = runningStream(); const sending = store.send('start')
    stream.emit({ type: 'permission_required', payload: { permission_id: 'p1' } })
    mocks.cancel.mockResolvedValue({ ...run(), pending_permission_id: 'p1', cancel_requested: true })
    await store.cancel()
    await store.respondPermission('once'); expect(mocks.permission).not.toHaveBeenCalled()
    expect(store.activeRun?.id).toBe(501); expect(store.loading).toBe(true); expect(stream.signal().aborted).toBe(false)
    await store.send('duplicate'); expect(mocks.stream).toHaveBeenCalledTimes(1)
    stream.emit({ type: 'cancelled', run_id: 501, payload: {} }); stream.pending.resolve(); await sending
    expect(store.activeRun).toBeNull(); expect(store.messages.at(-1)?.status).toBe('cancelled')
  })
  it('does not let a late cancellation response abort a new workspace task', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const old = runningStream(); const first = store.send('old')
    const pending = deferred<AssistantRunRecord>(); mocks.cancel.mockReturnValue(pending.promise)
    const cancelling = store.cancel(); await store.bindWorkspace(2)
    const newer = runningStream(); const second = store.send('new')
    pending.resolve({ ...run(), status: 'cancelled' }); await cancelling
    expect(newer.signal().aborted).toBe(false); expect(store.loading).toBe(true); expect(store.activeRun?.id).toBe(501)
    old.pending.resolve(); await first
    newer.emit({ type: 'done', run_id: 501, payload: {} }); newer.pending.resolve(); await second
  })
  it('does not restore a finished run when its permission response arrives late', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const stream = runningStream(); const sending = store.send('start')
    stream.emit({ type: 'permission_required', payload: { permission_id: 'p1', title: 'Patch file', tool: { name: 'apply_patch', input: { path: 'a.txt' } } } })
    expect(store.messages.at(-1)?.permission?.tool).toEqual({ name: 'apply_patch', input: { path: 'a.txt' } })
    const pending = deferred<AssistantRunRecord>(); mocks.permission.mockReturnValue(pending.promise)
    const answering = store.respondPermission('once'); stream.emit({ type: 'done', run_id: 501, payload: {} })
    pending.resolve(run()); await answering; stream.pending.resolve(); await sending
    expect(mocks.permission).toHaveBeenCalledWith(501, 'p1', 'once'); expect(store.activeRun).toBeNull(); expect(store.messages.at(-1)?.permission).toBeUndefined()
  })
  it('keeps a newer permission request when the previous response finishes late', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1)
    const stream = runningStream(); const sending = store.send('start')
    stream.emit({ type: 'permission_required', payload: { permission_id: 'p1' } })
    const pending = deferred<AssistantRunRecord>(); mocks.permission.mockReturnValue(pending.promise); const answering = store.respondPermission('reject')
    stream.emit({ type: 'permission_required', payload: { permission_id: 'p2' } }); pending.resolve(run()); await answering
    expect(store.activeRun?.permissionId).toBe('p2'); expect(store.messages.at(-1)?.permission?.permission_id).toBe('p2')
    stream.emit({ type: 'cancelled', run_id: 501, payload: {} }); stream.pending.resolve(); await sending
  })
  it('gates expert send, selection, handoff and permission answers after role removal', async () => {
    const store = useAssistantStore(); await store.bindWorkspace(1); mocks.workspace.canWrite = false
    await store.selectMode('expert'); expect(store.currentMode).toBe('fast')
    await store.handoff(1); expect(mocks.handoff).not.toHaveBeenCalled()
    store.currentMode = 'expert'; await store.send('forbidden'); expect(mocks.stream).not.toHaveBeenCalled()
    store.activeRun = { id: 501, messageId: 601, permissionId: 'p1' }; await store.respondPermission('once'); expect(mocks.permission).not.toHaveBeenCalled()
  })
})
