import { z } from 'zod'

/** 创建工作空间表单校验 */
export const createWorkspaceSchema = z.object({
  name: z.string().trim().min(1, '请输入工作空间名称').max(128, '名称过长'),
  description: z.string().max(512, '描述过长').optional(),
})

export type CreateWorkspaceInput = z.infer<typeof createWorkspaceSchema>

/** 更新工作空间表单 */
export const updateWorkspaceSchema = z.object({
  name: z.string().trim().min(1).max(128).optional(),
  description: z.string().max(512).optional(),
})

export type UpdateWorkspaceInput = z.infer<typeof updateWorkspaceSchema>

/** 邀请成员表单 */
export const inviteMemberSchema = z.object({
  username: z.string().trim().min(1, '请输入用户名'),
  role: z.enum(['admin', 'editor', 'viewer']),
})

export type InviteMemberInput = z.infer<typeof inviteMemberSchema>

/** 后端契约：工作空间详情 */
export const workspaceInfoSchema = z.object({
  id: z.number().int().positive(),
  name: z.string(),
  description: z.string().nullable().optional(),
  owner_id: z.number().int(),
  created_at: z.string().nullable().optional(),
  updated_at: z.string().nullable().optional(),
  my_role: z.string().nullable().optional(),
  member_count: z.number().int().optional(),
  members: z.array(z.object({
    id: z.number().int(),
    workspace_id: z.number().int(),
    user_id: z.number().int(),
    role: z.string(),
    username: z.string().optional(),
    avatar: z.string().nullable().optional(),
    joined_at: z.string().nullable().optional(),
  })).optional(),
})

/** 后端契约：工作空间列表包装 */
export const workspaceListResponseSchema = z.object({
  workspaces: z.array(workspaceInfoSchema),
  code: z.literal(200),
})

/** 成员列表响应 */
export const memberListResponseSchema = z.object({
  members: z.array(z.object({
    id: z.number().int(),
    workspace_id: z.number().int(),
    user_id: z.number().int(),
    role: z.string(),
    username: z.string().optional(),
    avatar: z.string().nullable().optional(),
    joined_at: z.string().nullable().optional(),
  })),
  code: z.literal(200),
})

export type WorkspaceInfo = z.infer<typeof workspaceInfoSchema>
export type WorkspaceListResult = z.infer<typeof workspaceListResponseSchema>
export type MemberInfo = z.infer<typeof memberListResponseSchema>['members'][number]
