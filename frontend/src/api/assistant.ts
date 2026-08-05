/** Assistant API contracts and HTTP helpers. */

import request from './request'

export type AssistantMode = 'fast' | 'expert'

export interface AssistantConversation {
  id: number
  workspace_id: number
  user_id: number
  mode: AssistantMode
  title: string | null
  status: string
  source_conversation_id: number | null
  opencode_runtime_id: number | null
  opencode_session_id: string | null
  created_at: string | null
  updated_at: string | null
  last_message_at: string | null
}

export interface AssistantMessageRecord {
  id: number
  conversation_id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  status: 'pending' | 'streaming' | 'completed' | 'failed' | 'cancelled'
  provider_message_id: string | null
  metadata: Record<string, unknown>
  usage: Record<string, unknown>
  sequence: number
  created_at: string | null
  updated_at: string | null
}

export interface AssistantRunRecord {
  id: number
  conversation_id: number
  user_message_id: number | null
  assistant_message_id: number | null
  engine: AssistantMode
  status: string
  request_id: string | null
  opencode_session_id: string | null
  pending_permission_id: string | null
  cancel_requested: boolean
  error_code: string | null
  error_message: string | null
  metadata: Record<string, unknown>
  started_at: string | null
  completed_at: string | null
  created_at: string | null
}

export interface AssistantEvent {
  type: string
  run_id?: number
  message_id?: number
  payload: Record<string, unknown>
}

export interface AssistantMessageResponse {
  conversation: AssistantConversation
  messages: AssistantMessageRecord[]
}

export interface AssistantConversationUpdate {
  title?: string
  status?: 'active' | 'archived'
}

export const listAssistantConversations = (mode?: AssistantMode, includeArchived = true) =>
  request.get<{ conversations: AssistantConversation[] }>('/assistant/conversations', {
    params: { ...(mode ? { mode } : {}), include_archived: includeArchived },
  })

export const createAssistantConversation = (mode: AssistantMode, title?: string) =>
  request.post<AssistantConversation>('/assistant/conversations', {
    mode,
    title: title || undefined,
  })

export const getAssistantMessages = (conversationId: number) =>
  request.get<AssistantMessageResponse>(`/assistant/conversations/${conversationId}/messages`)

export const updateAssistantConversation = (
  conversationId: number,
  payload: AssistantConversationUpdate,
) => request.patch<AssistantConversation>(`/assistant/conversations/${conversationId}`, payload)

export const deleteAssistantConversation = (conversationId: number) =>
  request.delete<void>(`/assistant/conversations/${conversationId}`)

export const getActiveAssistantRun = (conversationId: number) =>
  request.get<AssistantRunRecord | null>(`/assistant/conversations/${conversationId}/active-run`)

export const getActiveExpertRun = () =>
  request.get<AssistantRunRecord | null>('/assistant/runs/active')

export const cancelAssistantRun = (runId: number) =>
  request.post<AssistantRunRecord>(`/assistant/runs/${runId}/cancel`)

export const respondAssistantPermission = (
  runId: number,
  permissionId: string,
  response: 'once' | 'reject',
) =>
  request.post<AssistantRunRecord>(
    `/assistant/runs/${runId}/permissions/${encodeURIComponent(permissionId)}`,
    { response, remember: false },
  )

export const handoffAssistantConversation = (conversationId: number, messageId?: number) =>
  request.post<AssistantConversation>(`/assistant/conversations/${conversationId}/handoff`, {
    target_mode: 'expert',
    message_id: messageId,
  })
