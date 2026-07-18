import request from './request'

/** 后端契约：创建分享请求体；expires_at 省略表示永久有效 */
export interface CreateShareParams {
    file_id: number
    expires_at?: string
}

/** 后端契约：分享记录；token 用于公开下载链接，file_name 仅列表侧可能填充 */
export interface ShareInfo {
    id: number
    token: string
    file_id: number
    expires_at: string
    created_at: string
    file_name?: string
}

export const createShare = (data: CreateShareParams) => {
    return request.post<ShareInfo>('/share', data)
}

export const getShareInfo = (token: string) => {
    return request.get<ShareInfo>(`/share/${token}`)
}

export const getMyShares = () => {
    return request.get<ShareInfo[]>('/share/my')
}

export const cancelShare = (id: number) => {
    return request.delete<void>(`/share/${id}`)
}
