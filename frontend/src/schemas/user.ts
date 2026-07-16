import {z} from 'zod'

export const passwordChangeSchema = z
    .object({
        oldPassword: z.string().min(1, '请输入旧密码'),
        newPassword: z.string().min(6, '新密码至少 6 位').max(128, '密码过长'),
        confirmPassword: z.string().min(1, '请再次输入新密码'),
    })
    .refine((d) => d.newPassword === d.confirmPassword, {
        message: '两次输入的新密码不一致',
        path: ['confirmPassword'],
    })

export type PasswordChangeInput = z.infer<typeof passwordChangeSchema>
