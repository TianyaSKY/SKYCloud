import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'

import type { AssistantEvent } from '@/api/assistant'

interface SseRequestOptions {
  signal?: AbortSignal
  onEvent: (event: AssistantEvent) => void
}

/** Fetch an assistant stream with the same auth/workspace headers as Axios. */
export async function sseRequest(
  path: string,
  body: Record<string, unknown>,
  options: SseRequestOptions,
): Promise<void> {
  const auth = useAuthStore()
  const workspace = useWorkspaceStore()
  const response = await fetch(`/api${path}`, {
    method: 'POST',
    headers: {
      Authorization: auth.token ? `Bearer ${auth.token}` : '',
      'X-Workspace-Id': workspace.currentWorkspace ? String(workspace.currentWorkspace.id) : '',
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
    signal: options.signal,
  })

  if (!response.ok) {
    let detail = `Assistant request failed (${response.status})`
    try {
      const payload = (await response.json()) as { detail?: string; message?: string }
      detail = payload.message || payload.detail || detail
    } catch {
      // The status text remains the useful fallback for proxy failures.
    }
    throw new Error(detail)
  }
  if (!response.body) throw new Error('Assistant response has no stream')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const dispatch = (line: string) => {
    if (!line.startsWith('data:')) return
    const raw = line.slice(5).trim()
    if (!raw) return
    try {
      const event = JSON.parse(raw) as AssistantEvent
      if (event && typeof event.type === 'string') options.onEvent(event)
    } catch {
      // Ignore malformed provider data; the backend contract is JSON SSE.
    }
  }

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    lines.forEach(dispatch)
  }
  if (buffer) dispatch(buffer)
}

