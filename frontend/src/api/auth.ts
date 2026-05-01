import request from './request'

export const login = (data: any) => {
    return request.post('/auth/login', data)
}

export const register = (data: any) => {
    return request.post('/auth/register', data)
}

export const generateMcpToken = () => {
    return request.post('/auth/mcp-token')
}

export const createMcpToken = (data: { name?: string }) => {
    return request.post('/auth/mcp-token', data)
}

export const listMcpTokens = () => {
    return request.get('/auth/mcp-tokens')
}

export const revokeMcpToken = (id: number) => {
    return request.delete(`/auth/mcp-tokens/${id}`)
}
