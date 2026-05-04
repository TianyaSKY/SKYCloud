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
