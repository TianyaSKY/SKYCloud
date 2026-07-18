import {z} from 'zod'

export const createWorkspaceSchema = z.object({
    name: z.string().trim().min(1, '请输入工作区名称').max(120, '名称过长'),
})

export type CreateWorkspaceInput = z.infer<typeof createWorkspaceSchema>

export const workspaceInfoSchema = z.object({
    id: z.number().int().positive(),
    user_id: z.number().int().positive(),
    name: z.string().min(1).max(120),
    container_id: z.string().nullable(),
    status: z.enum(['creating', 'running', 'stopped', 'error']),
    error_message: z.string().nullable(),
    access_url: z.string().url().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
})

export const workspaceListResponseSchema = z.object({
    workspaces: z.array(workspaceInfoSchema),
    code: z.literal(200),
})

export type WorkspaceInfo = z.infer<typeof workspaceInfoSchema>
export type WorkspaceListResult = z.infer<typeof workspaceListResponseSchema>
