/** Cookie transport bypasses the access-token request interceptor. */
export interface BrowserSession {
  token: string
  user_id: number
  role: string
  expires_in: number
  user: { id: number; username: string; role: string; avatar?: string }
}

async function sessionRequest(path: string, token?: string | null): Promise<Response> {
  return fetch('/api/auth/' + path, {
    method: 'POST', credentials: 'same-origin', headers: {
      'X-Skycloud-Session': '1', ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    signal: AbortSignal.timeout(10000),
  })
}

export async function withBrowserSessionLock<T>(execute: () => Promise<T>): Promise<T> {
  return navigator.locks ? await navigator.locks.request('skycloud-auth-cookie', execute) : await execute()
}

export async function refreshBrowserSession(): Promise<BrowserSession | null> {
  const execute = async () => {
    const response = await sessionRequest('refresh')
    if (response.status === 401) return null
    if (!response.ok) throw new Error('登录会话暂时无法续期')
    const result = await response.json() as BrowserSession
    if (typeof result.token !== 'string' || !result.token || !result.user ||
        typeof result.user.id !== 'number' || typeof result.user.username !== 'string' ||
        typeof result.user.role !== 'string') throw new Error('登录会话响应无效')
    return result
  }
  // Read the shared cookie only after taking the origin-scoped browser lock.
  return withBrowserSessionLock(execute)
}

export async function logoutBrowserSession(token?: string | null): Promise<void> {
  const execute = async () => {
    const response = await sessionRequest('logout', token)
    if (!response.ok) throw new Error('退出登录暂时失败，请重试')
  }
  await withBrowserSessionLock(execute)
}
