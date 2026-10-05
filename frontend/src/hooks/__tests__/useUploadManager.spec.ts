import { effectScope, ref, type EffectScope } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUploadManager } from '../useUploadManager'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
const mocks = vi.hoisted(() => ({ upload: vi.fn(), info: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn(), remove: vi.fn() }))
vi.mock('../../utils/upload', () => ({ uploadFileOptimized: mocks.upload }))
vi.mock('@arco-design/web-vue', () => ({ Notification: mocks, Message: { error: vi.fn() } }))
vi.mock('@/router', () => ({ default: { currentRoute: { value: { name: 'home' } }, push: vi.fn() } }))
const scopes: EffectScope[] = []
function manager(parent = ref<number | null>(15), fetchFiles = vi.fn(async () => {})) {
  const scope = effectScope(); scopes.push(scope)
  return { ...scope.run(() => useUploadManager(parent, fetchFiles))!, scope, fetchFiles, parent }
}
const files = (count: number) => Array.from({ length: count }, (_, index) => new File(['file'], `file-${index}.txt`))
function deferred() { let resolve!: (value: { instantUpload: boolean }) => void; const promise = new Promise<{ instantUpload: boolean }>(yes => { resolve = yes }); return { promise, resolve } }
beforeEach(() => {
  vi.resetAllMocks(); setActivePinia(createPinia()); localStorage.clear()
  useAuthStore().setAuth('access', { id: 7 }); useWorkspaceStore().currentWorkspace = { id: 11, name: 'space', owner_id: 7 }
  mocks.upload.mockResolvedValue({ instantUpload: false })
})
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()) })

describe('upload batch lifetime and concurrency', () => {
  it('holds the original folder for queued files and limits all uploads to three slots', async () => {
    const pending: ReturnType<typeof deferred>[] = []; let active = 0; let maximum = 0
    mocks.upload.mockImplementation(() => { active++; maximum = Math.max(maximum, active); const request = deferred(); pending.push(request); return request.promise.finally(() => { active-- }) })
    const uploader = manager(); const starting = uploader.startUpload(files(7))
    await flushPromises(); expect(mocks.upload).toHaveBeenCalledTimes(3); uploader.parent.value = 99
    pending.splice(0).forEach(request => request.resolve({ instantUpload: false })); await flushPromises()
    expect(mocks.upload).toHaveBeenCalledTimes(6)
    pending.splice(0).forEach(request => request.resolve({ instantUpload: false })); await flushPromises()
    pending.splice(0).forEach(request => request.resolve({ instantUpload: false })); await starting
    expect(maximum).toBe(3); expect(mocks.upload.mock.calls.every(call => call[1] === 15)).toBe(true)
    expect(uploader.fetchFiles).toHaveBeenCalledTimes(1); expect(mocks.success.mock.calls[0]?.[0].content).toContain('成功 7')
  })
  it('does not revive a cancelled old queue when a new batch starts', async () => {
    const pending: ReturnType<typeof deferred>[] = []
    mocks.upload.mockImplementation(() => { const request = deferred(); pending.push(request); return request.promise })
    const uploader = manager(); const old = uploader.startUpload(files(8)); await flushPromises()
    const signals = mocks.upload.mock.calls.map(call => call[3].signal as AbortSignal)
    uploader.cancelAll(); expect(signals.every(signal => signal.aborted)).toBe(true)
    const newer = uploader.startUpload([new File(['new'], 'new.txt')]); await flushPromises()
    expect(mocks.upload).toHaveBeenCalledTimes(3)
    pending.splice(0).forEach(request => request.resolve({ instantUpload: false })); await flushPromises()
    expect(mocks.upload).toHaveBeenCalledTimes(4); expect(mocks.upload.mock.calls[3]?.[0].name).toBe('new.txt')
    pending.splice(0).forEach(request => request.resolve({ instantUpload: false })); await Promise.all([old, newer])
    expect(mocks.success).toHaveBeenCalledTimes(1); expect(uploader.fetchFiles).toHaveBeenCalledTimes(1); expect(mocks.warning).not.toHaveBeenCalled()
  })
  it.each(['workspace', 'login', 'dispose'])('aborts HTTP signals and queued uploads on %s change', async kind => {
    const pending: ReturnType<typeof deferred>[] = []
    mocks.upload.mockImplementation(() => { const request = deferred(); pending.push(request); return request.promise })
    const uploader = manager(); const starting = uploader.startUpload(files(8)); await flushPromises()
    if (kind === 'workspace') useWorkspaceStore().currentWorkspace = { id: 12, name: 'other', owner_id: 7 }
    else if (kind === 'login') useAuthStore().setAuth('new-family', { id: 7 })
    else uploader.scope.stop()
    expect(mocks.upload.mock.calls.every(call => call[3].signal.aborted)).toBe(true)
    pending.forEach(request => request.resolve({ instantUpload: false })); await starting
    expect(mocks.upload).toHaveBeenCalledTimes(3); expect(mocks.success).not.toHaveBeenCalled(); expect(uploader.fetchFiles).not.toHaveBeenCalled(); expect(mocks.error).not.toHaveBeenCalled()
  })
  it('does not accept a late start callback after the component scope was disposed', async () => {
    const uploader = manager(); uploader.scope.stop(); await uploader.startUpload(files(1))
    expect(mocks.upload).not.toHaveBeenCalled(); expect(mocks.info).not.toHaveBeenCalled()
  })
  it('preserves uploads when the same login profile changes without a new session', async () => {
    const pending = deferred(); mocks.upload.mockReturnValue(pending.promise)
    const uploader = manager(); const starting = uploader.startUpload(files(1)); await flushPromises()
    useAuthStore().setUser({ username: 'updated' })
    expect(mocks.upload.mock.calls[0]?.[3].signal.aborted).toBe(false)
    pending.resolve({ instantUpload: true }); await starting; expect(mocks.success.mock.calls[0]?.[0].title).toBe('秒传完成')
  })
  it('cancels batches waiting for a global slot and never dispatches them later', async () => {
    const pending: ReturnType<typeof deferred>[] = []
    mocks.upload.mockImplementation(() => { const request = deferred(); pending.push(request); return request.promise })
    const uploader = manager(); const first = uploader.startUpload(files(3)); await flushPromises()
    const waiting = uploader.startUpload(files(2)); await flushPromises(); uploader.cancelAll()
    pending.forEach(request => request.resolve({ instantUpload: false })); await Promise.all([first, waiting])
    expect(mocks.upload).toHaveBeenCalledTimes(3); expect(mocks.success).not.toHaveBeenCalled()
  })
  it('does not describe a committed upload as failed when refreshing the list fails', async () => {
    const refresh = vi.fn().mockRejectedValue(new Error('list unavailable')); const uploader = manager(ref(15), refresh)
    await uploader.startUpload(files(1)); expect(mocks.success).toHaveBeenCalledTimes(1); expect(mocks.error).not.toHaveBeenCalled(); expect(refresh).toHaveBeenCalledTimes(1)
  })
  it('reports actual success/failure counts and uses distinct IDs for simultaneous batches', async () => {
    mocks.upload.mockResolvedValueOnce({ instantUpload: false }).mockRejectedValueOnce(new Error('denied')).mockResolvedValue({ instantUpload: false })
    const uploader = manager(); await uploader.startUpload(files(3))
    expect(mocks.warning.mock.calls[0]?.[0].content).toContain('成功 2，失败 1')
    vi.clearAllMocks(); mocks.upload.mockResolvedValue({ instantUpload: false })
    await Promise.all([uploader.startUpload(files(1)), uploader.startUpload(files(1))])
    expect(new Set(mocks.success.mock.calls.map(call => call[0].id)).size).toBe(2)
  })
})
