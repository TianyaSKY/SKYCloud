import axios from 'axios'
import {type AxiosRequestConfig} from 'axios'
import {Message} from '@arco-design/web-vue'
import router from '../router'
import {useAuthStore} from '../stores/auth'
import {logger} from '../utils/logger'

const service = axios.create({
    baseURL: '/api',
    timeout: 10000
})

// 通过模块扩展让实例方法直接返回业务数据 Promise<T>，
// 配合响应拦截器 (response) => response.data 的解包行为。
declare module 'axios' {
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
    code?: number
    data?: unknown

    constructor(message: string, status: number, code?: number, data?: unknown) {
        super(message)
        this.name = 'BizError'
        this.status = status
        this.code = code
        this.data = data
    }
}

// 请求拦截器：从 store 注入 Authorization
service.interceptors.request.use(
    (config) => {
        const auth = useAuthStore()
        if (auth.token) {
            config.headers.Authorization = `Bearer ${auth.token}`
        }
        return config
    },
    (error) => Promise.reject(error)
)

// 响应拦截器：成功解包为业务数据；失败统一 Message 提示并归一为 BizError。
// 此处是全局唯一的错误提示点，调用方不再重复弹消息。
service.interceptors.response.use(
    (response) => response.data,
    (error) => {
        const status: number = error?.response?.status ?? 0
        const data: unknown = error?.response?.data
        const payload = data as { message?: string; detail?: string; code?: number } | undefined
        const msg = payload?.message || payload?.detail || '网络错误'
        const code = payload?.code
        const url = error?.config?.url ?? ''

        if (status === 401) {
            const auth = useAuthStore()
            const onLoginRoute = router.currentRoute.value.name === 'login'
            if (msg === 'Invalid username or password') {
                Message.error({content: '用户名或密码错误', position: 'top', closable: true})
            } else if (onLoginRoute) {
                Message.error({content: '登录失败，请检查账号密码', position: 'top', closable: true})
            } else {
                Message.error({content: '登录已过期，请重新登录', position: 'top', closable: true})
                auth.logout()
                router.push('/')
            }
        } else {
            Message.error({content: msg, position: 'top', closable: true})
        }

        logger.warn('HTTP 请求失败 url={} status={} code={} msg={}', url, status, code, msg)
        return Promise.reject(new BizError(msg, status, code, data))
    }
)

export default service
