import {z} from 'zod'

/** 登录表单：仅非空与长度，不做密码复杂度 */
export const loginSchema = z.object({
    username: z.string().min(1, '请输入用户名').max(64, '用户名过长'),
    password: z.string().min(1, '请输入密码').max(128, '密码过长'),
})

/** 注册表单：密码至少 6 位，且与确认密码一致；confirmPassword 仅前端字段 */
export const registerSchema = z
    .object({
        username: z.string().min(1, '请输入用户名').max(64, '用户名过长'),
        password: z.string().min(6, '密码至少 6 位').max(128, '密码过长'),
        confirmPassword: z.string().min(1, '请再次输入密码'),
    })
    .refine((d) => d.password === d.confirmPassword, {
        message: '两次输入的密码不一致',
        path: ['confirmPassword'],
    })

export type LoginInput = z.infer<typeof loginSchema>
export type RegisterInput = z.infer<typeof registerSchema>
