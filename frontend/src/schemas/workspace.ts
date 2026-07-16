import {z} from 'zod'

export const createWorkspaceSchema = z.object({
    name: z.string().trim().min(1, '请输入工作区名称').max(120, '名称过长'),
})

export type CreateWorkspaceInput = z.infer<typeof createWorkspaceSchema>
