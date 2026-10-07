import { apiErrorDetail } from '../utils/apiError'
import axios, { type AxiosRequestConfig } from 'axios'
import { Message } from '@arco-design/web-vue'
import router from '../router'
import { useAuthStore } from '../stores/auth'
import { useWorkspaceStore } from '../stores/workspace'
import { logger } from '../utils/logger'
import { assertRequestContext, captureRequestContext, type RequestContext } from '../utils/requestContext'

const service = axios.create({
  baseURL: '/api',
  timeout: 10000,
})

// 通过模块扩展让实例方法直接返回业务数据 Promise<T>，
// 配合响应拦截器 (response) => response.data 的解包行为。
declare module 'axios' {
  export interface AxiosRequestConfig {
    skycloudContext?: RequestContext
    sessionRetried?: boolean
  }
  export interface AxiosInstance {
    get<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T>
    delete<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T>
    post<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>
    put<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>
    patch<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>
  }
}

/**
 * 业务异常：响应拦截器把 axios 错误归一为此类型后 reject。
 * 调用方捕获后可通过 status/code/data 获取上下文，无需再读取 axios 原始 error。
 */
export class BizError extends Error {
  status: number
  code?: string
  data?: unknown

  constructor(message: string, status: number, code?: string, data?: unknown) {
    super(message)
    this.name = 'BizError'
    this.status = status
    this.code = code
    this.data = data
  }
}

// 请求拦截器：从 store 注入 Authorization 和 X-Workspace-Id
service.interceptors.request.use(
  (config) => {
    config.skycloudContext ??= captureRequestContext()
    assertRequestContext(config.skycloudContext)
    const auth = useAuthStore()
    if (auth.token) {
      config.headers.Authorization = `Bearer ${auth.token}`
    } else config.headers.delete('Authorization')
    if (config.skycloudContext.workspaceId !== null) {
      config.headers['X-Workspace-Id'] = String(config.skycloudContext.workspaceId)
    } else config.headers.delete('X-Workspace-Id')
    return config
  },
  (error) => Promise.reject(error),
)

// 响应拦截器：成功解包为业务数据；失败统一 Message 提示并归一为 BizError。
// 此处是全局唯一的错误提示点，调用方不再重复弹消息。
service.interceptors.response.use(
  (response) => {
    if (response.config.skycloudContext) assertRequestContext(response.config.skycloudContext)
    return response.data
  },
  async (error) => {
    if (axios.isCancel(error)) return Promise.reject(error)
    const status: number = error?.response?.status ?? 0
    const config = error?.config as AxiosRequestConfig | undefined
    if (config?.skycloudContext) assertRequestContext(config.skycloudContext)
    if (status === 401 && config && !config.sessionRetried && !config.url?.startsWith('/auth/login')) {
      config.sessionRetried = true
      const auth = useAuthStore()
      try {
        const used = String(config.headers?.Authorization || '')
        const fresh = auth.token && used !== `Bearer ${auth.token}`
        if (fresh || await auth.restore(true)) return service.request(config)
      } catch {
        return Promise.reject(new BizError('登录会话暂时无法续期，请重试', 503))
      }
    }
    let data: unknown = error?.response?.data
    if (data instanceof Blob && data.size <= 65536 && data.type.includes('json')) {
      const parsingContext = captureRequestContext()
      try { data = JSON.parse(await data.text()) } catch { /* Keep the bounded original payload. */ }
      assertRequestContext(parsingContext)
    }
    const { message: msg, code } = apiErrorDetail(data, '网络错误')
    const url = error?.config?.url ?? ''

    if (status === 401) {
      const auth = useAuthStore()
      const onLoginRoute = router.currentRoute.value.name === 'login'
      if (msg === 'Invalid username or password') {
        Message.error({ content: '用户名或密码错误', position: 'top', closable: true })
      } else if (onLoginRoute) {
        Message.error({ content: '登录失败，请检查账号密码', position: 'top', closable: true })
      } else {
        Message.error({ content: '登录已过期，请重新登录', position: 'top', closable: true })
        auth.clear()
        useWorkspaceStore().reset()
        router.push('/')
      }
    } else {
      Message.error({ content: msg, position: 'top', closable: true })
    }

    logger.warn('HTTP 请求失败 url={} status={} code={} msg={}', url, status, code, msg)
    return Promise.reject(new BizError(msg, status, code, data))
  },
)

export default service
