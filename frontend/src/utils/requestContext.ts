import { CanceledError } from 'axios'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'

export interface RequestContext {
  workspaceId: number | null
  userId: number | null
  sessionVersion: number
}

export function captureRequestContext(): RequestContext {
  const auth = useAuthStore()
  return { workspaceId: useWorkspaceStore().currentWorkspace?.id ?? null,
    userId: auth.user.id, sessionVersion: auth.sessionVersion }
}

export function assertRequestContext(context: RequestContext, signal?: AbortSignal) {
  const current = captureRequestContext()
  if (signal?.aborted || context.workspaceId !== current.workspaceId || context.userId !== current.userId || context.sessionVersion !== current.sessionVersion) {
    throw new CanceledError('请求上下文已改变或操作已取消')
  }
}
