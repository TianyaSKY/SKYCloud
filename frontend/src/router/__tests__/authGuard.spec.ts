import { describe, expect, it, vi } from 'vitest'
import { guardAuthenticatedRoute } from '../authGuard'

describe('authenticated routing', () => {
  it('waits for cookie restoration before checking identity or loading workspaces', async () => {
    let finish!: () => void
    const auth = { isAuthenticated: false, isAdmin: false, restore: vi.fn(() => new Promise<boolean>(resolve => {
      finish = () => { auth.isAuthenticated = true; resolve(true) }
    })) }
    const spaces = { workspaces: [], loadWorkspaces: vi.fn().mockResolvedValue(undefined) }
    const result = guardAuthenticatedRoute({path:'/home', meta:{requiresAuth:true}}, auth, spaces)
    expect(spaces.loadWorkspaces).not.toHaveBeenCalled()
    finish(); expect(await result).toBe(true)
    expect(spaces.loadWorkspaces).toHaveBeenCalledTimes(1)
  })
  it('redirects unauthenticated and non-admin users without loading protected data', async () => {
    const auth = {isAuthenticated:false,isAdmin:false,restore:vi.fn().mockResolvedValue(false)}
    const spaces = {workspaces:[],loadWorkspaces:vi.fn()}
    expect(await guardAuthenticatedRoute({path:'/home',meta:{requiresAuth:true}},auth,spaces)).toEqual({path:'/'})
    auth.isAuthenticated = true
    expect(await guardAuthenticatedRoute({path:'/admin',meta:{requiresAuth:true,requiresAdmin:true}},auth,spaces)).toEqual({path:'/home'})
    expect(spaces.loadWorkspaces).not.toHaveBeenCalled()
  })
  it('allows public landing during an outage and returns a restored login to home', async () => {
    const auth = {isAuthenticated:false,isAdmin:false,restore:vi.fn().mockRejectedValue(new Error('offline'))}
    const spaces = {workspaces:[],loadWorkspaces:vi.fn()}
    expect(await guardAuthenticatedRoute({path:'/',meta:{}},auth,spaces)).toBe(true)
    expect(await guardAuthenticatedRoute({path:'/home',meta:{requiresAuth:true}},auth,spaces)).toEqual({path:'/'})
    auth.restore.mockResolvedValue(true);auth.isAuthenticated = true
    expect(await guardAuthenticatedRoute({path:'/',meta:{}},auth,spaces)).toEqual({path:'/home'})
  })
})
