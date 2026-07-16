import {z} from 'zod'

export const sysDictSchema = z.object({
    key: z.string().trim().min(1, '请输入配置键').max(128, '键过长'),
    value: z.string().min(1, '请输入配置值').max(2048, '值过长'),
    des: z.string().max(512, '描述过长').optional().or(z.literal('')),
    enable: z.boolean(),
})

export type SysDictInput = z.infer<typeof sysDictSchema>
