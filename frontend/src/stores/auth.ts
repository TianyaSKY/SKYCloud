import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { logger } from '../utils/logger'

export interface UserInfo {
  id: number | null
  username: string
  role: string
  avatar?: string
}

const TOKEN_KEY = 'token'
const USER_KEY = 'user'

const EMPTY_USER: UserInfo = { id: null, username: '', role: 'user' }

/** 安全解析 localStorage 中的 user JSON，损坏时返回空用户而非抛错 */
function safeParseUser(str: string | null): UserInfo {
  if (!str) return { ...EMPTY_USER }
  try {
    const obj = JSON.parse(str)
    return {
      id: obj.id ?? null,
      username: obj.username ?? '',
      role: obj.role ?? 'user',
      avatar: obj.avatar ?? '',
    }
  } catch {
    return { ...EMPTY_USER }
  }
}

/** 安全写入 localStorage：隐私模式 / 配额超限 / 禁用时仅记录日志，不中断内存状态更新 */
function safeSetItem(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch (err) {
    logger.warn('localStorage 写入失败 key={} err={}', key, err)
  }
}

/** 安全移除 localStorage：禁用或异常时仅记录日志，不影响内存状态清空 */
function safeRemoveItem(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch (err) {
    logger.warn('localStorage 移除失败 key={} err={}', key, err)
  }
}

/**
 * 集中管理鉴权状态（token + 用户信息），替代此前散落在各组件的 localStorage 读写。
 * 路由守卫、侧边栏菜单、请求拦截器统一消费此 store，登录态变化可响应式刷新。
 */
export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const user = ref<UserInfo>(safeParseUser(localStorage.getItem(USER_KEY)))

  const isAuthenticated = computed(() => !!token.value)
  const isAdmin = computed(() => user.value.role === 'admin')

  function setAuth(t: string, u: Partial<UserInfo>) {
    token.value = t
    safeSetItem(TOKEN_KEY, t)
    setUser(u)
  }

  function setUser(u: Partial<UserInfo>) {
    user.value = { ...user.value, ...u }
    safeSetItem(USER_KEY, JSON.stringify(user.value))
  }

  function logout() {
    token.value = null
    user.value = { ...EMPTY_USER }
    safeRemoveItem(TOKEN_KEY)
    safeRemoveItem(USER_KEY)
  }

  return { token, user, isAuthenticated, isAdmin, setAuth, setUser, logout }
})
