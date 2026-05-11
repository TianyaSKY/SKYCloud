import request from './request'

export interface WorkspaceInfo {
  id: number
  user_id: number
  name: string
  container_id: string | null
  status: 'creating' | 'running' | 'stopped' | 'error'
  error_message: string | null
  access_url: string | null
  created_at: string
  updated_at: string
}

export interface McpSetupResult {
  success: boolean
  message: string
  mcp_url: string
  token_id: number
  config_path: string
}

export const listWorkspaces = () => {
  return request.get('/workspace')
}

export const getWorkspace = (id: number) => {
  return request.get(`/workspace/${id}`)
}

export const createWorkspace = (data: { name: string }) => {
  return request.post('/workspace', data)
}

export const startWorkspace = (id: number) => {
  return request.post(`/workspace/${id}/start`)
}

export const stopWorkspace = (id: number) => {
  return request.post(`/workspace/${id}/stop`)
}

export const deleteWorkspace = (id: number) => {
  return request.delete(`/workspace/${id}`)
}

export const restartWorkspace = (id: number) => {
  return request.post(`/workspace/${id}/restart`, null, { timeout: 30000 })
}

export const setupMcpConnection = (id: number) => {
  return request.post<McpSetupResult>(`/workspace/${id}/setup-mcp`, null, { timeout: 30000 })
}
