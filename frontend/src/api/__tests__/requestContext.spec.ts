import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { AxiosError } from 'axios'
import request from '../request'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import { captureRequestContext } from '@/utils/requestContext'
import { refreshBrowserSession } from '@/utils/authSession'
import router from '@/router'
import { Message } from '@arco-design/web-vue'
vi.mock('@/router', () => ({ default: { currentRoute: { value: { name: 'home' } }, push: vi.fn() } }))
vi.mock('@arco-design/web-vue', () => ({ Message: { error: vi.fn() } }))
vi.mock('@/utils/authSession', () => ({ refreshBrowserSession: vi.fn(), logoutBrowserSession: vi.fn() }))
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
const fresh = { token: 'fresh', user_id: 7, role: 'common', expires_in: 900, user: { id: 7, username: 'user', role: 'common' } }
beforeEach(() => {
  vi.resetAllMocks(); setActivePinia(createPinia()); localStorage.clear()
  useAuthStore().setAuth('access', { id: 7 }); useWorkspaceStore().currentWorkspace = { id: 11, name: 'space', owner_id: 7 }
})
describe('request identity and workspace boundaries', () => {
  it('rejects an explicitly captured upload context before dispatch into another workspace', async () => {
    const context = captureRequestContext(); useWorkspaceStore().currentWorkspace = { id: 12, name: 'other', owner_id: 7 }
    const adapter = vi.fn(); request.defaults.adapter = adapter
    await expect(request.post('/files/multipart/init', {}, { skycloudContext: context })).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    expect(adapter).not.toHaveBeenCalled(); expect(Message.error).not.toHaveBeenCalled()
  })
  it('rejects stale successful responses rather than hydrating a new workspace with old data', async () => {
    const pending = deferred<string>(); const adapter = vi.fn(async config => ({ status: 200, statusText: 'OK', headers: {}, config, data: await pending.promise }))
    request.defaults.adapter = adapter; const old = request.get('/files/list'); const rejected = expect(old).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    await vi.waitFor(() => expect(adapter).toHaveBeenCalledTimes(1)); useWorkspaceStore().currentWorkspace = { id: 12, name: 'other', owner_id: 7 }
    pending.resolve('old private files'); await rejected
  })
  it.each([401, 403])('does not let late HTTP %s errors clear or notify a newer login of the same user', async status => {
    const pending = deferred<void>(); const adapter = vi.fn(async config => { await pending.promise; throw new AxiosError('old request', 'ERR_BAD_REQUEST', config, undefined, { status, statusText: 'Denied', headers: {}, config, data: { detail: 'old denial' } }) })
    request.defaults.adapter = adapter; const old = request.get('/protected'); const rejected = expect(old).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    await vi.waitFor(() => expect(adapter).toHaveBeenCalledTimes(1)); useAuthStore().setAuth('new-login', { id: 7 }); pending.resolve(); await rejected
    expect(useAuthStore().token).toBe('new-login'); expect(Message.error).not.toHaveBeenCalled(); expect(router.push).not.toHaveBeenCalled(); expect(refreshBrowserSession).not.toHaveBeenCalled()
  })
  it.each(['workspace', 'session'])('does not replay a rejected mutation after %s changes during refresh', async kind => {
    const pending = deferred<typeof fresh>(); vi.mocked(refreshBrowserSession).mockReturnValue(pending.promise)
    const adapter = vi.fn(async config => { throw new AxiosError('expired', 'ERR_BAD_REQUEST', config, undefined, { status: 401, statusText: 'Unauthorized', headers: {}, config, data: { detail: 'expired' } }) })
    request.defaults.adapter = adapter; const old = request.post('/files', 'old-upload'); const rejected = expect(old).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    await vi.waitFor(() => expect(refreshBrowserSession).toHaveBeenCalledTimes(1))
    if (kind === 'workspace') useWorkspaceStore().currentWorkspace = { id: 12, name: 'other', owner_id: 7 }
    else useAuthStore().setAuth('new-login', { id: 7 })
    pending.resolve(fresh); await rejected; expect(adapter).toHaveBeenCalledTimes(1)
    if (kind === 'session') expect(useAuthStore().token).toBe('new-login')
  })
  it.each([401, 403])('discards late HTTP %s Blob errors after the login changes during body parsing', async status => {
    const body = deferred<string>()
    const blob = new Blob(['{}'], { type: 'application/json' })
    const read = vi.fn(() => body.promise); Object.defineProperty(blob, 'text', { value: read })
    request.defaults.adapter = async config => { throw new AxiosError('old blob', 'ERR_BAD_REQUEST', config, undefined, { status, statusText: 'Denied', headers: {}, config, data: blob }) }
    const old = request.get('/download', { responseType: 'blob', sessionRetried: true })
    const rejected = expect(old).rejects.toMatchObject({ code: 'ERR_CANCELED' })
    await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(1))
    useAuthStore().setAuth('new-login', { id: 7 }); body.resolve('{"detail":"old denial"}'); await rejected
    expect(useAuthStore().token).toBe('new-login'); expect(Message.error).not.toHaveBeenCalled(); expect(router.push).not.toHaveBeenCalled()
  })
  it('permits a normal token refresh within the same session and original workspace', async () => {
    const context = captureRequestContext(); vi.mocked(refreshBrowserSession).mockResolvedValue(fresh)
    const adapter = vi.fn(async config => {
      expect(config.headers['X-Workspace-Id']).toBe('11')
      if (config.headers.Authorization === 'Bearer access') throw new AxiosError('expired', 'ERR_BAD_REQUEST', config, undefined, { status: 401, statusText: 'Unauthorized', headers: {}, config, data: {} })
      return { status: 200, statusText: 'OK', headers: {}, config, data: 'accepted' }
    })
    request.defaults.adapter = adapter
    expect(await request.post('/files', 'upload', { skycloudContext: context })).toBe('accepted')
    expect(adapter).toHaveBeenCalledTimes(2); expect(useAuthStore().sessionVersion).toBe(context.sessionVersion)
  })
})
