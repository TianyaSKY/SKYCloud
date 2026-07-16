import axios from 'axios'
import {Message} from '@arco-design/web-vue'
import router from '../router'
import {useAuthStore} from '../stores/auth'

const service = axios.create({
    baseURL: '/api',
    timeout: 10000
})

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

// 响应拦截器：统一映射业务异常为 Arco Message；401 时清登录态并跳登录
service.interceptors.response.use(
    (response) => response.data,
    (error) => {
        const status = error.response?.status
        const data = error.response?.data
        const msg = data?.message || data?.detail || '网络错误'

        if (status === 401) {
            const auth = useAuthStore()
            if (msg === 'Invalid username or password') {
                Message.error({
                    content: '用户名或密码错误',
                    position: 'top',
                    closable: true
                })
            } else if (router.currentRoute.value.name === 'login') {
                Message.error({
                    content: '登录失败，请检查账号密码',
                    position: 'top',
                    closable: true
                })
            } else {
                Message.error({
                    content: '登录已过期，请重新登录',
                    position: 'top',
                    closable: true
                })
                auth.logout()
                router.push('/')
            }
        } else {
            Message.error({
                content: msg,
                position: 'top',
                closable: true
            })
        }

        return Promise.reject(error)
    }
)

export default service
