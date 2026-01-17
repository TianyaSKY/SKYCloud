import axios from 'axios'
import {Message} from '@arco-design/web-vue'
import router from '../router'

const service = axios.create({
    baseURL: '/api',
    timeout: 10000
})

// 请求拦截器
service.interceptors.request.use(
    (config) => {
        const token = localStorage.getItem('token')
        if (token) {
            config.headers.Authorization = `Bearer ${token}`
        }
        return config
    },
    (error) => {
        return Promise.reject(error)
    }
)

// 响应拦截器
service.interceptors.response.use(
    (response) => {
        return response.data
    },
    (error) => {
        const status = error.response?.status
        const data = error.response?.data
        const msg = data?.message || data?.detail || '网络错误'

        if (status === 401) {
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
                localStorage.removeItem('token')
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
