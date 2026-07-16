import request from './request'
import type {LoginInput, RegisterInput} from '../schemas/auth'

/** 后端契约：/auth/login 返回，含 token 与基础用户标识 */
export interface LoginResult {
    token: string
    user_id: number
    role: string
}

/** 后端契约：/auth/register 返回，调用点未消费具体字段，仅可能携带提示信息 */
export interface RegisterResult {
    message?: string
}

/** 后端契约：/auth/mcp-tokens 列表项；新建 token 的完整凭证仅在创建时返回 */
export interface McpTokenRecord {
    id: number
    name: string
    token_preview: string
    created_at: string | null
    expires_at: string | null
    /** 状态标记由后端计算下发，缺失时视作未撤销/未过期 */
    is_revoked?: boolean
    is_expired?: boolean
}

/** 后端契约：/auth/mcp-token 创建结果，mcp_token 为完整凭证（仅此一次返回） */
export interface McpTokenCreated {
    mcp_token: string
    id?: number
    name?: string
    created_at?: string | null
    expires_at?: string | null
}

export interface CreateMcpTokenParams {
    name?: string
}

export const login = (data: LoginInput) => {
    return request.post<LoginResult>('/auth/login', data)
}

export const register = (data: RegisterInput) => {
    // confirmPassword 仅为前端校验字段，不发送给后端；保持注册载荷为 username + password
    const {username, password} = data
    return request.post<RegisterResult>('/auth/register', {username, password})
}

export const createMcpToken = (data: CreateMcpTokenParams) => {
    return request.post<McpTokenCreated>('/auth/mcp-token', data)
}

export const listMcpTokens = () => {
    return request.get<McpTokenRecord[]>('/auth/mcp-tokens')
}

export const revokeMcpToken = (id: number) => {
    return request.delete<void>(`/auth/mcp-tokens/${id}`)
}
