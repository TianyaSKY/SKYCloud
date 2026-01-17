import request from './request'

export interface CreateShareParams {
    file_id: number
    expires_at?: string
}

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
    return request.get(`/share/${token}`)
}

export const getMyShares = () => {
    return request.get<ShareInfo[]>('/share/my')
}

export const cancelShare = (id: number) => {
    return request.delete(`/share/${id}`)
}
