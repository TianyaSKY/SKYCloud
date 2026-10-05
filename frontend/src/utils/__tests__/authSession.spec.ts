import { afterEach, describe, expect, it, vi } from 'vitest'
import { refreshBrowserSession, logoutBrowserSession } from '../authSession'

afterEach(() => vi.unstubAllGlobals())
describe('HttpOnly session transport', () => {
  it('uses same-origin cookies and the CSRF header without an Authorization token', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ token: 'access',
      user: { id: 1, username: 'u', role: 'common' } }), {status: 200}))
    vi.stubGlobal('fetch', fetch)
    expect((await refreshBrowserSession())?.token).toBe('access')
    expect(fetch).toHaveBeenCalledWith('/api/auth/refresh', expect.objectContaining({
      method: 'POST', credentials: 'same-origin', headers: { 'X-Skycloud-Session': '1' } }))
  })
  it('separates revoked cookies from temporary server failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('', {status:401}))
      .mockResolvedValueOnce(new Response('', {status:503})))
    expect(await refreshBrowserSession()).toBeNull()
    await expect(refreshBrowserSession()).rejects.toThrow('暂时')
  })
  it('executes cookie mutations only inside the shared browser lock', async () => {
    let release!: () => Promise<void>
    const fetch = vi.fn().mockResolvedValue(new Response('{}', {status:200}))
    const locks = { request: vi.fn((_name, execute) => new Promise(resolve => {
      release = async () => { await execute(); resolve(undefined) }
    })) }
    vi.stubGlobal('navigator', {locks}); vi.stubGlobal('fetch', fetch)
    const pending = logoutBrowserSession()
    expect(fetch).not.toHaveBeenCalled()
    expect(locks.request).toHaveBeenCalledWith('skycloud-auth-cookie', expect.any(Function))
    await release(); await pending
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
