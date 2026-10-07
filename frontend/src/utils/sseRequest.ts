import { apiErrorDetail } from './apiError'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'

import type { AssistantEvent } from '@/api/assistant'

interface SseRequestOptions {
  signal?: AbortSignal
  onEvent: (event: AssistantEvent) => void
}

// Bound incomplete lines and multi-line events, including streams without separators.
const MAX_EVENT_CHARACTERS = 1024 * 1024

/** Fetch an assistant stream with the same auth/workspace headers as Axios. */
export async function sseRequest(
  path: string,
  body: Record<string, unknown>,
  options: SseRequestOptions,
): Promise<void> {
  const auth = useAuthStore()
  const workspace = useWorkspaceStore()
  const workspaceId = workspace.currentWorkspace?.id
  const userId = auth.user.id
  const sessionVersion = auth.sessionVersion
  const checkContext = () => {
    if (options.signal?.aborted || workspace.currentWorkspace?.id !== workspaceId || auth.user.id !== userId || auth.sessionVersion !== sessionVersion) {
      throw new DOMException('Assistant context changed', 'AbortError')
    }
  }
  const request = () => fetch(`/api${path}`, {
    method: 'POST',
    headers: {
      Authorization: auth.token ? `Bearer ${auth.token}` : '',
      'X-Workspace-Id': workspaceId ? String(workspaceId) : '',
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
    signal: options.signal,
  })
  checkContext()
  let response = await request()
  // Only retry a rejected request, never replay an accepted/partly consumed stream.
  if (response.status === 401) {
    await response.body?.cancel()
    if (await auth.restore(true)) {
      checkContext()
      response = await request()
    }
  }
  try { checkContext() } catch (error) {
    try { await response.body?.cancel() } catch { /* Preserve context rejection. */ }
    throw error
  }
  if (!response.ok) {
    let detail = `Assistant request failed (${response.status})`
    try {
      detail = apiErrorDetail(await response.json(), detail).message
    } catch {
      // The status remains the useful fallback for proxy failures.
    }
    throw new Error(detail)
  }
  if (!response.body) throw new Error('Assistant response has no stream')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let data: string[] = []
  let dataLength = 0
  const dispatch = () => {
    if (!data.length) return
    const raw = data.join('\n')
    data = []
    dataLength = 0
    let event: AssistantEvent
    try { event = JSON.parse(raw) as AssistantEvent } catch { return }
    checkContext()
    if (event && typeof event.type === 'string') options.onEvent(event)
  }
  const line = (value: string) => {
    if (value.endsWith('\r')) value = value.slice(0, -1)
    if (!value) { dispatch(); return }
    if (!value.startsWith('data:')) return
    const payload = value.slice(5).replace(/^ /, '')
    dataLength += payload.length + 1
    if (dataLength > MAX_EVENT_CHARACTERS) throw new Error('Assistant event exceeds the size limit')
    data.push(payload)
  }
  const consume = (text: string) => {
    buffer += text
    let newline: number
    while ((newline = buffer.indexOf('\n')) !== -1) {
      if (newline > MAX_EVENT_CHARACTERS) throw new Error('Assistant event exceeds the size limit')
      line(buffer.slice(0, newline))
      buffer = buffer.slice(newline + 1)
    }
    if (buffer.length + dataLength > MAX_EVENT_CHARACTERS) throw new Error('Assistant event exceeds the size limit')
  }
  const abort = () => { void reader.cancel().catch(() => {}) }
  options.signal?.addEventListener('abort', abort, { once: true })
  try {
    while (true) {
      checkContext()
      const { value, done } = await reader.read()
      checkContext()
      if (done) break
      consume(decoder.decode(value, { stream: true }))
    }
    consume(decoder.decode())
    if (buffer) line(buffer)
    dispatch()
  } finally {
    options.signal?.removeEventListener('abort', abort)
    try { await reader.cancel() } catch { /* Preserve the read/callback error. */ }
    reader.releaseLock()
  }
}
