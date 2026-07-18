import request from './request'
import type { LoginInput, RegisterInput } from '../schemas/auth'

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

/** 后端契约：/auth/mcp-token 元数据（不含完整凭证） */
export interface McpTokenMeta {
  id: number
  name: string
  token_preview: string
  created_at: string | null
  expires_at: string | null
  is_revoked?: boolean
  is_expired?: boolean
}

/** 后端契约：每用户唯一 MCP Token；mcp_token 为完整凭证，可随时复制 */
export interface McpTokenResult {
  mcp_token: string
  token: McpTokenMeta
  user_id: number
  expires_in_days?: number
  usage?: string
}

export const login = (data: LoginInput) => {
  return request.post<LoginResult>('/auth/login', data)
}

export const register = (data: RegisterInput) => {
  // confirmPassword 仅为前端校验字段，不发送给后端；保持注册载荷为 username + password
  const { username, password } = data
  return request.post<RegisterResult>('/auth/register', { username, password })
}

/** 获取当前用户唯一 MCP Token（不存在则后端自动签发） */
export const getMcpToken = () => {
  return request.get<McpTokenResult>('/auth/mcp-token')
}

/** 刷新唯一 MCP Token（旧 Token 立即失效，并同步运行中工作区） */
export const refreshMcpToken = () => {
  return request.post<McpTokenResult>('/auth/mcp-token/refresh')
}
