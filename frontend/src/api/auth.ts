import request from './request'

export const login = (data: any) => {
    return request.post('/auth/login', data)
}

export const register = (data: any) => {
    return request.post('/auth/register', data)
}
