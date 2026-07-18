import request from './request'
import {
  type CreateWorkspaceInput,
  type WorkspaceInfo,
  workspaceInfoSchema,
  workspaceListResponseSchema,
  type WorkspaceListResult,
} from '../schemas/workspace'

export type { WorkspaceInfo, WorkspaceListResult }

/** 列出当前用户工作区；响应用 Zod 校验后再返回 */
export const listWorkspaces = () => {
  return request.get<unknown>('/workspace').then(workspaceListResponseSchema.parse)
}

/** 获取单个工作区详情 */
export const getWorkspace = (id: number) => {
  return request.get<unknown>(`/workspace/${id}`).then(workspaceInfoSchema.parse)
}

/** 创建工作区 */
export const createWorkspace = (data: CreateWorkspaceInput) => {
  return request.post<unknown>('/workspace', data).then(workspaceInfoSchema.parse)
}

/** 启动工作区容器 */
export const startWorkspace = (id: number) => {
  return request.post<unknown>(`/workspace/${id}/start`).then(workspaceInfoSchema.parse)
}

/** 停止工作区容器 */
export const stopWorkspace = (id: number) => {
  return request.post<unknown>(`/workspace/${id}/stop`).then(workspaceInfoSchema.parse)
}

/** 删除工作区 */
export const deleteWorkspace = (id: number) => {
  return request.delete<void>(`/workspace/${id}`)
}

/** 重启工作区；超时放宽到 30s，容器冷启动可能较慢 */
export const restartWorkspace = (id: number) => {
  return request.post<unknown>(`/workspace/${id}/restart`, null, { timeout: 30000 }).then(workspaceInfoSchema.parse)
}
