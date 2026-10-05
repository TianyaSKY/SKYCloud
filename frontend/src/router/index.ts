import { createRouter, createWebHistory } from 'vue-router'
import { guardAuthenticatedRoute } from './authGuard'
import LoginView from '../views/LoginView.vue'
import { useAuthStore } from '../stores/auth'
import { useWorkspaceStore } from '../stores/workspace'

// ShareView 是公开页面，与 Login 一起作为首屏候选；其余页面按需懒加载，
// 避免所有用户初次加载即打包全部视图。
const HomeView = () => import('../views/HomeView.vue')
const ShareView = () => import('../views/ShareView.vue')
const MySharesView = () => import('../views/MySharesView.vue')
const InboxView = () => import('../views/InboxView.vue')
const SysDictView = () => import('../views/SysDictView.vue')
const DocView = () => import('../views/DocView.vue')
const TokenUsageView = () => import('../views/TokenUsageView.vue')
const AdminTokenUsageView = () => import('../views/AdminTokenUsageView.vue')
const WorkspaceManageView = () => import('../views/WorkspaceManageView.vue')

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'login',
      component: LoginView,
    },
    {
      path: '/home',
      name: 'home',
      component: HomeView,
      meta: { requiresAuth: true },
    },
    {
      path: '/shares',
      name: 'my-shares',
      component: MySharesView,
      meta: { requiresAuth: true },
    },
    {
      path: '/inbox',
      name: 'inbox',
      component: InboxView,
      meta: { requiresAuth: true },
    },
    {
      path: '/docs',
      name: 'docs',
      component: DocView,
      meta: { requiresAuth: true },
    },
    {
      path: '/token-usage',
      name: 'token-usage',
      component: TokenUsageView,
      meta: { requiresAuth: true },
    },
    {
      path: '/admin/token-usage',
      name: 'admin-token-usage',
      component: AdminTokenUsageView,
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: '/sys_dicts',
      name: 'sys-dicts',
      component: SysDictView,
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    {
      path: '/workspaces',
      name: 'workspaces',
      component: WorkspaceManageView,
      meta: { requiresAuth: true },
    },
    {
      path: '/s/:token',
      name: 'share',
      component: ShareView,
    },
  ],
})

router.beforeEach((to) => guardAuthenticatedRoute(to, useAuthStore(), useWorkspaceStore()))

export default router
