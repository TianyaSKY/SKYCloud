type Destination = { path: string; meta: { requiresAuth?: unknown; requiresAdmin?: unknown } }
type Auth = { restore: () => Promise<boolean>; isAuthenticated: boolean; isAdmin: boolean }
type Workspaces = { workspaces: unknown[]; loadWorkspaces: () => Promise<void> }

export async function guardAuthenticatedRoute(to: Destination, auth: Auth, workspaces: Workspaces) {
  try { await auth.restore() } catch {
    return to.meta.requiresAuth ? { path: '/' } : true
  }
  if (to.path === '/' && auth.isAuthenticated) return { path: '/home' }
  if (to.meta.requiresAuth && !auth.isAuthenticated) return { path: '/' }
  if (to.meta.requiresAdmin && !auth.isAdmin) return { path: '/home' }
  if (auth.isAuthenticated && workspaces.workspaces.length === 0 && to.meta.requiresAuth) {
    await workspaces.loadWorkspaces()
  }
  return true
}
