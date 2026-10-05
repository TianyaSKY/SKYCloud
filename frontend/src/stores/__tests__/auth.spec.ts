import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '../auth'
import { refreshBrowserSession, logoutBrowserSession } from '../../utils/authSession'
vi.mock('../../utils/authSession', () => ({ refreshBrowserSession: vi.fn(), logoutBrowserSession: vi.fn() }))
const restored = vi.mocked(refreshBrowserSession)
const revoked = vi.mocked(logoutBrowserSession)
const session = { token: 'memory-access', user_id: 7, role: 'admin', expires_in: 900,
  user: { id: 7, username: 'alice', role: 'admin' } }
beforeEach(() => { vi.resetAllMocks(); localStorage.clear(); setActivePinia(createPinia()) })

describe('memory authentication', () => {
  it('never restores historical localStorage credentials or persists a new token', () => {
    localStorage.setItem('token', 'historical-secret')
    localStorage.setItem('user', JSON.stringify(session.user))
    const auth = useAuthStore()
    expect(auth.token).toBeNull()
    expect(localStorage.getItem('token')).toBeNull()
    auth.setAuth('new-secret', session.user)
    auth.setUser({ username: 'updated' })
    expect(auth.token).toBe('new-secret')
    expect(localStorage.getItem('token')).toBeNull()
    expect(localStorage.getItem('user')).toBeNull()
  })
  it('coalesces initial restoration and hydrates authoritative profile', async () => {
    let complete!: (value: typeof session) => void
    restored.mockImplementation(() => new Promise(resolve => { complete = resolve }))
    const auth = useAuthStore()
    const first = auth.restore(); const second = auth.restore()
    expect(restored).toHaveBeenCalledTimes(1)
    complete(session)
    expect(await first).toBe(true); expect(await second).toBe(true)
    expect(auth.isAdmin).toBe(true)
    expect(auth.user.username).toBe('alice')
    await auth.restore()
    expect(restored).toHaveBeenCalledTimes(1)
  })
  it('cannot overwrite a newer login with a late refresh response', async () => {
    let complete!: (value: typeof session) => void
    restored.mockImplementation(() => new Promise(resolve => { complete = resolve }))
    const auth = useAuthStore(); const pending = auth.restore()
    auth.setAuth('new-account-token', { id: 8, username: 'bob', role: 'common' })
    complete(session); await pending
    expect(auth.token).toBe('new-account-token')
    expect(auth.user.id).toBe(8)
  })
  it('keeps transient refresh failures retryable and retains current state', async () => {
    const auth = useAuthStore(); auth.setAuth('current', session.user)
    restored.mockRejectedValueOnce(new Error('outage')).mockResolvedValueOnce(session)
    await expect(auth.restore(true)).rejects.toThrow('outage')
    expect(auth.token).toBe('current')
    expect(await auth.restore(true)).toBe(true)
    expect(auth.token).toBe('memory-access')
  })
  it('clears rejected sessions and only reports logout after server revocation', async () => {
    const auth = useAuthStore(); auth.setAuth('current', session.user)
    revoked.mockRejectedValueOnce(new Error('outage')).mockResolvedValueOnce()
    await expect(auth.logout()).rejects.toThrow('outage')
    expect(auth.isAuthenticated).toBe(true)
    await auth.logout(); expect(auth.isAuthenticated).toBe(false)
    expect(auth.user.id).toBeNull()
    auth.setAuth('another', session.user); restored.mockResolvedValue(null)
    expect(await auth.restore(true)).toBe(false)
  })
  it('does not silently adopt another tab account while retaining the previous workspace state', async () => {
    const auth = useAuthStore(); auth.setAuth('account-eight', {id:8,username:'bob',role:'common'})
    restored.mockResolvedValue(session)
    expect(await auth.restore(true)).toBe(false)
    expect(auth.token).toBeNull()
    expect(auth.user.id).toBeNull()
  })
})
