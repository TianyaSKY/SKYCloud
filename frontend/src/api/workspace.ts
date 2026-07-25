import request from './request'
import {
  type CreateWorkspaceInput,
  type UpdateWorkspaceInput,
  type InviteMemberInput,
  type WorkspaceInfo,
  type MemberInfo,
  workspaceInfoSchema,
  workspaceListResponseSchema,
  memberListResponseSchema,
  type WorkspaceListResult,
} from '../schemas/workspace'

export type { WorkspaceInfo, WorkspaceListResult, MemberInfo }

/** 列出当前用户的所有工作空间 */
export const listWorkspaces = () => {
  return request.get<unknown>('/workspace').then(workspaceListResponseSchema.parse)
}

/** 获取工作空间详情（含成员列表） */
export const getWorkspace = (id: number) => {
  return request.get<unknown>(`/workspace/${id}`).then(workspaceInfoSchema.parse)
}

/** 创建工作空间 */
export const createWorkspace = (data: CreateWorkspaceInput) => {
  return request.post<unknown>('/workspace', data).then(workspaceInfoSchema.parse)
}

/** 更新工作空间 */
export const updateWorkspace = (id: number, data: UpdateWorkspaceInput) => {
  return request.put<unknown>(`/workspace/${id}`, data).then(workspaceInfoSchema.parse)
}

/** 删除工作空间 */
export const deleteWorkspace = (id: number) => {
  return request.delete<void>(`/workspace/${id}`)
}

/** 邀请成员 */
export const inviteMember = (workspaceId: number, data: InviteMemberInput) => {
  return request.post<unknown>(`/workspace/${workspaceId}/members`, data)
}

/** 列出成员 */
export const listMembers = (workspaceId: number) => {
  return request.get<unknown>(`/workspace/${workspaceId}/members`).then(memberListResponseSchema.parse)
}

/** 变更成员角色 */
export const updateMemberRole = (workspaceId: number, userId: number, role: string) => {
  return request.put<unknown>(`/workspace/${workspaceId}/members/${userId}`, { role })
}

/** 移除成员 */
export const removeMember = (workspaceId: number, userId: number) => {
  return request.delete<void>(`/workspace/${workspaceId}/members/${userId}`)
}
