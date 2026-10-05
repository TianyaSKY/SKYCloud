import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { refreshBrowserSession, logoutBrowserSession } from '../utils/authSession'

export interface UserInfo {
  id: number | null
  username: string
  role: string
  avatar?: string
}
const EMPTY_USER: UserInfo = { id: null, username: '', role: 'user' }

export const useAuthStore = defineStore('auth', () => {
  // Remove historical credentials without reading them back into memory.
  try { localStorage.removeItem('token'); localStorage.removeItem('user') } catch { /* Storage may be disabled. */ }
  const token = ref<string | null>(null)
  const user = ref<UserInfo>({ ...EMPTY_USER })
  const isAuthenticated = computed(() => !!token.value)
  const isAdmin = computed(() => user.value.role === 'admin')
  let initialized = false
  let generation = 0
  let pending: Promise<boolean> | undefined

  function clear() {
    generation += 1
    initialized = true
    token.value = null
    user.value = { ...EMPTY_USER }
  }
  function setAuth(t: string, u: Partial<UserInfo>) {
    generation += 1
    initialized = true
    token.value = t
    user.value = { ...EMPTY_USER, ...u }
  }
  function setUser(u: Partial<UserInfo>) { user.value = { ...user.value, ...u } }

  async function restore(force = false): Promise<boolean> {
    if (!force && initialized) return isAuthenticated.value
    if (pending) return pending
    const current = generation
    pending = (async () => {
      const result = await refreshBrowserSession()
      if (current !== generation) return isAuthenticated.value
      initialized = true
      if (result) {
        if (user.value.id !== null && user.value.id !== result.user.id) {
          clear()
          return false
        }
        token.value = result.token
        user.value = result.user
      } else clear()
      return isAuthenticated.value
    })()
    try { return await pending } finally { pending = undefined }
  }

  async function logout() {
    // A failed server revocation remains retryable; do not claim logout.
    await logoutBrowserSession(token.value)
    clear()
  }

  return { token, user, isAuthenticated, isAdmin, setAuth, setUser, restore, logout, clear }
})
