import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useWorkspaceStore } from '../workspace'
import { useAuthStore } from '../auth'
import { listWorkspaces, type WorkspaceInfo, type WorkspaceListResult } from '@/api/workspace'
vi.mock('@/api/workspace', () => ({ listWorkspaces: vi.fn() }))
vi.mock('@/utils/authSession', () => ({ refreshBrowserSession: vi.fn(), logoutBrowserSession: vi.fn() }))
const list = vi.mocked(listWorkspaces)
const workspace = (id: number, my_role = 'editor'): WorkspaceInfo => ({ id, name: `space-${id}`, owner_id: 1, my_role })
const result = (...workspaces: WorkspaceInfo[]): WorkspaceListResult => ({ code: 200, workspaces })
function deferred() {
  let resolve!: (value: WorkspaceListResult) => void
  const promise = new Promise<WorkspaceListResult>(complete => { resolve = complete })
  return { promise, resolve }
}
beforeEach(() => { vi.resetAllMocks(); localStorage.clear(); setActivePinia(createPinia()); useAuthStore().setAuth('a', { id: 1 }) })
describe('workspace membership and selection', () => {
  it('restores saved membership and applies authoritative role changes', async () => {
    localStorage.setItem('current_workspace_id', '2')
    const store = useWorkspaceStore(); list.mockResolvedValueOnce(result(workspace(1), workspace(2)))
    await store.loadWorkspaces(); expect(store.currentWorkspace?.id).toBe(2); expect(store.canWrite).toBe(true)
    list.mockResolvedValueOnce(result(workspace(1), workspace(2, 'viewer')))
    await store.loadWorkspaces(); expect(store.canWrite).toBe(false); expect(store.isAdmin).toBe(false)
  })
  it('drops removed membership and clears permissions when no spaces remain', async () => {
    const store = useWorkspaceStore(); store.switchWorkspace(workspace(2, 'admin'))
    list.mockResolvedValueOnce(result(workspace(1, 'viewer'))).mockResolvedValueOnce(result())
    await store.loadWorkspaces(); expect(store.currentWorkspace?.id).toBe(1); expect(store.canWrite).toBe(false)
    await store.loadWorkspaces(); expect(store.currentWorkspace).toBeNull(); expect(store.workspaces).toEqual([])
    expect(localStorage.getItem('current_workspace_id')).toBeNull(); expect(store.isAdmin).toBe(false)
  })
  it('retains the most recent selection when a list request completes', async () => {
    const store = useWorkspaceStore(); store.switchWorkspace(workspace(1))
    const pending = deferred(); list.mockReturnValue(pending.promise)
    const loading = store.loadWorkspaces(); store.switchWorkspace(workspace(2))
    pending.resolve(result(workspace(1), workspace(2, 'viewer'))); await loading
    expect(store.currentWorkspace?.id).toBe(2); expect(store.canWrite).toBe(false)
  })
  it('discards older list responses and gives loading ownership to the latest request', async () => {
    const old = deferred(); const latest = deferred(); list.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const store = useWorkspaceStore(); const first = store.loadWorkspaces(); const second = store.loadWorkspaces()
    old.resolve(result(workspace(1, 'admin'))); await first; expect(store.loading).toBe(true); expect(store.workspaces).toEqual([])
    latest.resolve(result(workspace(2, 'viewer'))); await second; expect(store.loading).toBe(false); expect(store.currentWorkspace?.id).toBe(2)
  })
  it.each(['reset', 'logout', 'replacement-login'])('invalidates permissions and pending responses on %s', async action => {
    const store = useWorkspaceStore(); store.switchWorkspace(workspace(1, 'admin'))
    const pending = deferred(); list.mockReturnValue(pending.promise); const load = store.loadWorkspaces()
    if (action === 'reset') store.reset()
    else if (action === 'logout') useAuthStore().clear()
    else useAuthStore().setAuth('new-login', { id: 1 })
    expect(store.currentWorkspace).toBeNull(); expect(store.loading).toBe(false); expect(store.canWrite).toBe(false)
    pending.resolve(result(workspace(1, 'admin'))); await load; expect(store.workspaces).toEqual([])
  })
  it('continues without localStorage and does not fall back to a different selected space', async () => {
    const store = useWorkspaceStore()
    const read = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    try {
      list.mockResolvedValue(result(workspace(1), workspace(2)))
      await store.loadWorkspaces(); store.switchWorkspace(workspace(2)); await store.loadWorkspaces()
      expect(store.currentWorkspace?.id).toBe(2); expect(store.loading).toBe(false)
    } finally { read.mockRestore(); write.mockRestore() }
  })
  it('preserves current membership on a retryable list failure', async () => {
    const store = useWorkspaceStore(); store.switchWorkspace(workspace(1)); list.mockRejectedValueOnce(new Error('outage'))
    await store.loadWorkspaces(); expect(store.currentWorkspace?.id).toBe(1); expect(store.loading).toBe(false)
    list.mockResolvedValueOnce(result(workspace(1, 'viewer'))); await store.loadWorkspaces(); expect(store.canWrite).toBe(false)
  })
})
